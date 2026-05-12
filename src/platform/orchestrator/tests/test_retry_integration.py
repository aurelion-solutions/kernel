# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end integration test for the retry REST path (Step 19).

Strategy:
- Register a no-op action and build a PipelineDefinition in-memory (no YAML).
- Insert source run directly (no route POST — pipeline must be in app.state for
  that, which is fiddly to set up; direct insert is cleaner for integration).
- Drive runner → source becomes completed.
- POST /pipeline-runs/{id}/retry → 201; drive runner → retry becomes completed.
- GET /pipeline-runs → both rows present; verify retry_of_run_id linkage.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus, PipelineTriggerSource
from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionContext, register_action
from src.platform.orchestrator.runner import WorkerIdentity, run_one_iteration
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Action schemas
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    pass


class _NoResult(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DictLoader:
    def __init__(self, mapping: dict[tuple[str, int], PipelineDefinition]) -> None:
        self._mapping = mapping

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        return self._mapping.get((name, version))


def _make_worker() -> WorkerIdentity:
    return WorkerIdentity(worker_id='retry-integ-0', hostname='retry-host', pid=1, slot_index=0)


async def _run_one(
    session_factory: async_sessionmaker[AsyncSession],
    loader: _DictLoader,
    capturing: CapturingEventService,
) -> str:
    return await run_one_iteration(
        session_factory,
        worker=_make_worker(),
        pipeline_loader=loader,
        events=EventService(sink=capturing),
        logs=NoOpLogService(),
    )


async def _insert_pending(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    *,
    pipeline_name: str,
) -> PipelineRun:
    async with session_factory() as session:
        svc = PipelineOrchestratorService(
            session=session,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='retry-integ-corr',
        )
        await session.commit()
        return result.run


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


async def test_retry_endpoint_creates_new_run_that_runner_completes(
    app: FastAPI,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Full path: source inserted → completed via runner → retry via REST → completed via runner."""
    # Repair module-level binding in case a sibling test ran importlib.reload.
    import src.platform.orchestrator.registry as _reg_mod  # noqa: PLC0415

    if _reg_mod.ACTION_REGISTRY is not ACTION_REGISTRY:
        _reg_mod.ACTION_REGISTRY = ACTION_REGISTRY

    ACTION_REGISTRY._clear_for_tests()
    capturing = CapturingEventService()

    @register_action('retry_integ', 'noop', _NoArgs, _NoResult)
    async def _noop(args: _NoArgs, ctx: ActionContext) -> dict:
        return {}

    try:
        defn = PipelineDefinition(
            name='retry_pipe',
            version=1,
            schema_version=1,
            source_path=tmp_path / 'retry_pipe.yaml',
            content_hash='retry_integ_hash_001',
            args_schema_dict={},
            triggers=(),
            steps=(
                {
                    'name': 'noop_step',
                    'kind': 'engine_call',
                    'engine': 'retry_integ',
                    'action': 'noop',
                    'args': {},
                },
            ),
            raw_dict={},
        )
        loader = _DictLoader({('retry_pipe', 1): defn})

        # 1. Insert source run directly (bypasses route — no pipeline def needed in app.state).
        source = await _insert_pending(session_factory, capturing, pipeline_name='retry_pipe')
        capturing.clear()

        # 2. Drive runner → source becomes completed.
        outcome = await _run_one(session_factory, loader, capturing)
        assert outcome == 'completed', f'Expected completed but got {outcome}'

        # Verify source is completed.
        async with session_factory() as session:
            source_row = await session.execute(
                sa.select(PipelineRun).where(PipelineRun.id == source.id).execution_options(populate_existing=True)
            )
            source_run = source_row.scalar_one()
            assert source_run.status == PipelineRunStatus.completed
            assert source_run.retry_of_run_id is None

        # 3. POST /pipeline-runs/{id}/retry → 201.
        capturing.clear()
        retry_resp = await client.post(f'/api/v0/pipeline-runs/{source.id}/retry')
        assert retry_resp.status_code == 201, retry_resp.text
        retry_body = retry_resp.json()
        retry_id = retry_body['run_id']
        assert retry_body['retry_of_run_id'] == str(source.id)
        assert retry_body['status'] == 'pending'

        # 4. Drive runner one more iteration → retry becomes completed.
        capturing.clear()
        outcome2 = await _run_one(session_factory, loader, capturing)
        assert outcome2 == 'completed', f'Expected completed but got {outcome2}'

        # Verify retry row is completed.
        async with session_factory() as session:
            retry_row = await session.execute(
                sa.select(PipelineRun).where(PipelineRun.id == retry_id).execution_options(populate_existing=True)
            )
            retry_run = retry_row.scalar_one()
            assert retry_run.status == PipelineRunStatus.completed
            assert str(retry_run.retry_of_run_id) == str(source.id)

        # 5. GET /pipeline-runs → both rows present.
        list_resp = await client.get('/api/v0/pipeline-runs')
        assert list_resp.status_code == 200
        run_ids = {r['id'] for r in list_resp.json()}
        assert str(source.id) in run_ids
        assert retry_id in run_ids

    finally:
        ACTION_REGISTRY._clear_for_tests()
