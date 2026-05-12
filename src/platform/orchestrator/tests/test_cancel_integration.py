# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end integration test for the cancel watcher path (Step 18).

Strategy (deterministic — no real wall clock):
- Register a long-running fake action that waits for a local ``asyncio.Event``
  that we control from the test, so we never actually sleep 20 s.
- Inject the cancel_event into _heartbeat_refresher by subclassing / patching
  so that the refresher fires immediately without waiting 3 s.
- Assert that run_one_iteration returns 'cancelled' and the DB row is
  status='cancelled'.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
)
from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionContext, register_action
from src.platform.orchestrator.runner import WorkerIdentity, run_one_iteration
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Pydantic schemas for synthetic long-running action
# ---------------------------------------------------------------------------


class _WaitArgs(BaseModel):
    pass


class _WaitResult(BaseModel):
    pass


# ---------------------------------------------------------------------------
# DictLoader
# ---------------------------------------------------------------------------


class _DictLoader:
    def __init__(self, mapping: dict[tuple[str, int], PipelineDefinition]) -> None:
        self._mapping = mapping

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        return self._mapping.get((name, version))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker() -> WorkerIdentity:
    return WorkerIdentity(worker_id='cancel-integ-0', hostname='cancel-host', pid=1, slot_index=0)


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
            correlation_id='integ-cancel-corr',
        )
        await session.commit()
        return result.run


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


class TestCancelIntegration:
    async def test_cancel_while_step_running(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """End-to-end: cancel_event set by a patched refresher → run cancelled."""
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()

        # Synchronisation primitive: unblocked by the test's background task.
        _action_started = asyncio.Event()
        _action_proceed = asyncio.Event()

        @register_action('cancel_integ', 'wait', _WaitArgs, _WaitResult)
        async def _wait_action(args: _WaitArgs, ctx: ActionContext) -> dict:
            _action_started.set()
            await asyncio.wait_for(_action_proceed.wait(), timeout=10.0)
            return {}

        try:
            defn = PipelineDefinition(
                name='cancel_pipe',
                version=1,
                schema_version=1,
                source_path=tmp_path / 'cancel_pipe.yaml',
                content_hash='integ_cancel_hash',
                args_schema_dict={},
                triggers=(),
                steps=(
                    {
                        'name': 'slow_step',
                        'kind': 'engine_call',
                        'engine': 'cancel_integ',
                        'action': 'wait',
                        'args': {},
                    },
                ),
                raw_dict={},
            )
            loader = _DictLoader({('cancel_pipe', 1): defn})
            run = await _insert_pending(
                session_factory,
                capturing,
                pipeline_name='cancel_pipe',
            )
            capturing.clear()

            # We patch _heartbeat_refresher to use interval_seconds=0 so it
            # fires immediately.  After the first tick the refresher reads
            # the run status; if it's 'cancelling' it sets cancel_event.
            # We set the DB status to 'cancelling' from a background task once
            # the action has started.

            import src.platform.orchestrator.runner as _runner_mod  # noqa: PLC0415

            original_refresher = _runner_mod._heartbeat_refresher

            async def _fast_refresher(
                sf: async_sessionmaker[AsyncSession],
                *,
                run_id,
                worker_id,
                events,
                logs,
                stop_event: asyncio.Event,
                cancel_event: asyncio.Event,
                interval_seconds: float = 3.0,
            ) -> None:
                """Override: use 0.05 s interval to drive the watcher without real delays."""
                return await original_refresher(
                    sf,
                    run_id=run_id,
                    worker_id=worker_id,
                    events=events,
                    logs=logs,
                    stop_event=stop_event,
                    cancel_event=cancel_event,
                    interval_seconds=0.05,
                )

            _runner_mod._heartbeat_refresher = _fast_refresher  # type: ignore[attr-defined]

            async def _set_cancelling_in_db() -> None:
                """Wait for action to start, then set DB status to cancelling."""
                await asyncio.wait_for(_action_started.wait(), timeout=5.0)
                async with session_factory() as session:
                    await session.execute(
                        sa.update(PipelineRun)
                        .where(PipelineRun.id == run.id)
                        .values(status=PipelineRunStatus.cancelling)
                        .execution_options(synchronize_session=False)
                    )
                    await session.commit()

            bg_task = asyncio.create_task(_set_cancelling_in_db())

            try:
                outcome = await asyncio.wait_for(
                    run_one_iteration(
                        session_factory,
                        worker=_make_worker(),
                        pipeline_loader=loader,
                        events=EventService(sink=capturing),
                        logs=NoOpLogService(),
                    ),
                    timeout=10.0,
                )
            finally:
                _runner_mod._heartbeat_refresher = original_refresher  # type: ignore[attr-defined]
                _action_proceed.set()  # unblock action if still running
                bg_task.cancel()
                try:
                    await bg_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 # allowed-broad: test fixture cleanup
                    pass

            assert outcome == 'cancelled'

            # DB row must be in cancelled state.
            async with session_factory() as session:
                row = await session.execute(
                    sa.select(PipelineRun).where(PipelineRun.id == run.id).execution_options(populate_existing=True)
                )
                final_run = row.scalar_one()

            assert final_run.status == PipelineRunStatus.cancelled
            assert final_run.finished_at is not None

            # pipeline.run.cancelled event must be emitted.
            cancelled_events = capturing.filter_by_type('pipeline.run.cancelled')
            assert len(cancelled_events) == 1
            assert cancelled_events[0].payload['previous_status'] == 'cancelling'

        finally:
            ACTION_REGISTRY._clear_for_tests()
