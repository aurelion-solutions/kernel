# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Race test — two simulated executor nodes × 4 slots each (8 concurrent
worker loops) against 100 pre-seeded pending ``PipelineRun`` rows.

Physical setup: 8 asyncio tasks with ``slot_index`` 0..7 share one event
loop and one PostgreSQL connection pool.  The "two-node" framing is logical —
the real contention surface is ``SELECT … FOR UPDATE SKIP LOCKED`` inside
PostgreSQL, which is process-agnostic.  The assumption holds as long as each
worker opens its own session (which ``work_loop`` already does — every claim
calls ``session_factory()`` independently).  If ``runner.py::claim_one_pending_run``
ever gains an in-process mutex, this proxy collapses — see the function
docstring for the design constraint.

Assertions (Phase 18 Step 22 acceptance gate):
1. All 100 runs reach ``completed`` status.
2. Zero runs remain in any non-terminal status.
3. 100 ``step_runs`` rows with ``status='completed'``.
4. 100 distinct ``pipeline_run_id`` values in ``step_runs`` (no duplicates).
5. ``pipeline.run.started`` emitted exactly 100 times.
6. ``pipeline.run.completed`` emitted exactly 100 times.
7. At least 4 distinct ``worker_id`` values in ``pipeline_runs``
   (fairness floor — each of the 4 slot_index values claimed ≥ 1 run).

Wall-clock budget: 30 s (enforced via ``asyncio.wait_for``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from pydantic import BaseModel
import pytest
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
    StepRun,
    StepRunStatus,
)
from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionContext, register_action
from src.platform.orchestrator.runner import work_loop
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Synthetic action schemas
# ---------------------------------------------------------------------------


class _NoopArgs(BaseModel):
    i: int  # only here to vary content_hash; body never reads it


class _NoopResult(BaseModel):
    pass


# ---------------------------------------------------------------------------
# In-process pipeline loader backed by a dict
# ---------------------------------------------------------------------------


class _DictLoader:
    """Simple dict-backed pipeline lookup for integration tests."""

    def __init__(self, mapping: dict[tuple[str, int], PipelineDefinition]) -> None:
        self._mapping = mapping

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        return self._mapping.get((name, version))


# ---------------------------------------------------------------------------
# Race test
# ---------------------------------------------------------------------------


class TestRaceTwoNodesFourSlots:
    @pytest.mark.timeout(60)
    async def test_100_runs_executed_exactly_once(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """8 concurrent ``work_loop`` tasks claim and execute 100 pending runs.

        No run is executed twice (``FOR UPDATE SKIP LOCKED`` holds under
        contention).  At least 4 distinct slot_index values claim at least
        one run (fairness floor).
        """
        # --- 1. Registry reset ---
        ACTION_REGISTRY._clear_for_tests()

        # --- 2. Synthetic action ---
        @register_action('race', 'noop', _NoopArgs, _NoopResult)
        async def _noop(args: _NoopArgs, ctx: ActionContext) -> dict[str, Any]:
            return {}

        # --- 3. Pipeline definition ---
        defn = PipelineDefinition(
            name='race_pipe',
            version=1,
            schema_version=1,
            source_path=tmp_path / 'race_pipe.yaml',
            content_hash='race_pipe_hash_001',
            args_schema_dict={
                'type': 'object',
                'properties': {'i': {'type': 'integer'}},
            },
            triggers=(),
            steps=(
                {
                    'name': 'noop_step',
                    'kind': 'engine_call',
                    'engine': 'race',
                    'action': 'noop',
                    'args': {'i': '${args.i}'},
                },
            ),
            raw_dict={},
        )
        loader = _DictLoader({('race_pipe', 1): defn})

        # --- 4. Seed 100 pending runs (unique args to avoid content_hash collision) ---
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = PipelineOrchestratorService(
                session=session,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
            )
            for i in range(100):
                await svc.create_pipeline_run(
                    pipeline_name='race_pipe',
                    pipeline_version=1,
                    args={'i': i},
                    trigger_source=PipelineTriggerSource.http,
                    correlation_id=f'race-{i}',
                )
            await session.commit()

        # Discard seed events — we only care about events from the workers.
        capturing.clear()

        # --- 5–6. Shared event service + shutdown event ---
        events = EventService(sink=capturing)
        shutdown_event = asyncio.Event()

        # --- 7. Spawn 8 worker tasks (slot_index 0..7) ---
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(
                work_loop(
                    session_factory=session_factory,
                    pipeline_loader=loader,
                    events=events,
                    logs=NoOpLogService(),
                    slot_index=slot,
                    shutdown_event=shutdown_event,
                    poll_interval=0.1,
                    drain_timeout=15.0,
                )
            )
            for slot in range(8)
        ]

        # --- 8. Drain poller ---
        residual_histogram: dict[str, int] = {}

        async def _drain_poller() -> None:
            while True:
                async with session_factory() as s:
                    result = await s.execute(
                        sa.text(
                            'SELECT COUNT(*) FROM pipeline_runs '
                            "WHERE status IN ('pending','running','awaiting_event','cancelling')"
                        )
                    )
                    in_flight: int = result.scalar_one()
                if in_flight == 0:
                    shutdown_event.set()
                    return
                await asyncio.sleep(0.2)

        poller_task = asyncio.create_task(_drain_poller())

        # --- 9. Await all with hard cap ---
        try:

            async def _run_all() -> None:
                results = await asyncio.gather(poller_task, *tasks, return_exceptions=True)
                for exc in results:
                    if exc is not None and not isinstance(exc, asyncio.CancelledError):
                        pytest.fail(f'Worker/poller raised: {exc!r}')

            await asyncio.wait_for(_run_all(), timeout=30.0)

            # --- 10. Assertions (inside try, before cleanup deletes rows) ---
            async with session_factory() as s:
                completed_runs: int = (
                    await s.execute(
                        sa.select(sa.func.count(PipelineRun.id)).where(
                            PipelineRun.status == PipelineRunStatus.completed
                        )
                    )
                ).scalar_one()

                non_completed_runs: int = (
                    await s.execute(
                        sa.select(sa.func.count(PipelineRun.id)).where(
                            PipelineRun.status != PipelineRunStatus.completed
                        )
                    )
                ).scalar_one()

                completed_steps: int = (
                    await s.execute(
                        sa.select(sa.func.count(StepRun.id)).where(StepRun.status == StepRunStatus.completed)
                    )
                ).scalar_one()

                distinct_run_ids_in_steps: int = (
                    await s.execute(sa.select(sa.func.count(sa.func.distinct(StepRun.pipeline_run_id))))
                ).scalar_one()

                worker_id_rows = (await s.execute(sa.select(PipelineRun.worker_id))).scalars().all()

            worker_ids = {w for w in worker_id_rows if w is not None}

            started_events = capturing.filter_by_type('pipeline.run.started')
            completed_events = capturing.filter_by_type('pipeline.run.completed')

            assert completed_runs == 100, f'Expected 100 completed runs, got {completed_runs}'
            assert non_completed_runs == 0, f'Expected 0 non-completed runs, got {non_completed_runs}'
            assert completed_steps == 100, f'Expected 100 completed step_runs, got {completed_steps}'
            assert distinct_run_ids_in_steps == 100, (
                f'Expected 100 distinct pipeline_run_id in step_runs, got {distinct_run_ids_in_steps}'
            )
            assert len(started_events) == 100, f'Expected 100 pipeline.run.started events, got {len(started_events)}'
            assert len(completed_events) == 100, (
                f'Expected 100 pipeline.run.completed events, got {len(completed_events)}'
            )
            assert len(worker_ids) >= 4, (
                f'Fairness floor: expected ≥4 distinct worker_ids, got {len(worker_ids)}: {worker_ids}'
            )

        except TimeoutError:
            # Collect residual histogram for the assertion message.
            try:
                async with session_factory() as s:
                    rows = await s.execute(sa.text('SELECT status, COUNT(*) FROM pipeline_runs GROUP BY status'))
                    residual_histogram = {row[0]: row[1] for row in rows}
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                pass
            pytest.fail(f'Race test timed out after 30 s. Residual status histogram: {residual_histogram}')

        finally:
            # --- 11. Cleanup ---
            shutdown_event.set()  # defensive — idempotent
            alive = [t for t in tasks if not t.done()]
            if alive:
                for t in alive:
                    t.cancel()
                await asyncio.gather(*alive, return_exceptions=True)  # noqa: BLE001 # allowed-broad: test fixture cleanup

            if not poller_task.done():
                poller_task.cancel()
                try:
                    await poller_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 # allowed-broad: test fixture cleanup
                    pass

            ACTION_REGISTRY._clear_for_tests()

            # Sweep test rows so the 100-row footprint doesn't bleed into other tests.
            try:
                async with session_factory() as s:
                    await s.execute(sa.delete(StepRun))
                    await s.execute(sa.delete(PipelineRun))
                    await s.commit()
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                pass
