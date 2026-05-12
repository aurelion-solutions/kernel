# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PipelineOrchestratorService.reclaim_stale_run and list_stale_run_ids.

All tests use real PostgreSQL (via session_factory from root conftest).
Staleness is forced via raw SQL on last_heartbeat_at — no wall-clock sleeps.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
    StepRun,
    StepRunStatus,
)
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_A = 'host-reclaim-1-0'


def _make_svc(session: AsyncSession, capturing: CapturingEventService) -> PipelineOrchestratorService:
    return PipelineOrchestratorService(
        session=session,
        events=EventService(sink=capturing),
        logs=NoOpLogService(),
    )


async def _insert_running(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    *,
    pipeline_name: str = 'reclaim_pipe',
    worker_id: str = _WORKER_A,
) -> PipelineRun:
    """Insert a pending run and immediately mark it running."""
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='test-reclaim',
        )
        run = result.run
        await svc.mark_pipeline_running(run.id, worker_id=worker_id, correlation_id='test-reclaim')
        await session.commit()
        return run


async def _make_stale(session_factory: async_sessionmaker[AsyncSession], run_id: object) -> None:
    """Force last_heartbeat_at to 30 seconds ago to make the run stale."""
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE pipeline_runs SET last_heartbeat_at = now() - interval '30 seconds' WHERE id = :rid"),
            {'rid': run_id},
        )
        await session.commit()


async def _insert_running_step(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    run_id: object,
) -> StepRun:
    """Insert a running StepRun for the given pipeline run."""
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        step = await svc.create_step_run(run_id, 'step1', {}, correlation_id='test-reclaim')  # type: ignore[arg-type]
        await session.commit()
        return step


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReclaimStaleRun:
    async def test_fresh_heartbeat_returns_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """reclaim_stale_run on a run with a fresh heartbeat → False, no events."""
        capturing = CapturingEventService()
        run = await _insert_running(session_factory, capturing)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.reclaim_stale_run(run.id)
            await session.commit()

        assert result is False
        assert capturing.emitted == []

    async def test_stale_run_no_step_reclaimed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Stale run with no active StepRun → True, row reset to pending, heartbeat_lost event."""
        capturing = CapturingEventService()
        run = await _insert_running(session_factory, capturing)
        await _make_stale(session_factory, run.id)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.reclaim_stale_run(run.id, correlation_id='reclaim-corr')
            await session.commit()

        assert result is True

        # Row reset to pending.
        async with session_factory() as session:
            row = await session.get(PipelineRun, run.id)
        assert row is not None
        assert row.status == PipelineRunStatus.pending
        assert row.worker_id is None
        assert row.last_heartbeat_at is None
        assert row.started_at is None

        # Exactly one heartbeat_lost event, no step.aborted.
        event_types = [e.event_type for e in capturing.emitted]
        assert event_types == ['pipeline.run.heartbeat_lost']
        assert capturing.emitted[0].payload['previous_worker_id'] == _WORKER_A

    async def test_stale_run_with_running_step_reclaimed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Stale run with a running StepRun → True, step aborted, both events emitted in order."""
        capturing = CapturingEventService()
        run = await _insert_running(session_factory, capturing)
        step = await _insert_running_step(session_factory, capturing, run.id)
        await _make_stale(session_factory, run.id)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.reclaim_stale_run(run.id, correlation_id='reclaim-corr')
            await session.commit()

        assert result is True

        # StepRun → aborted.
        async with session_factory() as session:
            step_row = await session.get(StepRun, step.id)
        assert step_row is not None
        assert step_row.status == StepRunStatus.aborted
        assert step_row.error == 'reclaimed: heartbeat lost'
        assert step_row.finished_at is not None

        # Both events in order.
        event_types = [e.event_type for e in capturing.emitted]
        assert event_types == ['pipeline.run.heartbeat_lost', 'pipeline.step.aborted']

        aborted_evt = capturing.emitted[1]
        assert aborted_evt.payload['step_run_id'] == str(step.id)
        assert aborted_evt.payload['step_name'] == 'step1'
        assert aborted_evt.payload['attempt'] == 1
        assert aborted_evt.payload['reason'] == 'reclaimed: heartbeat lost'

    async def test_second_reclaim_returns_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Second call to reclaim_stale_run on already-pending row → False, no extra events."""
        capturing = CapturingEventService()
        run = await _insert_running(session_factory, capturing)
        await _make_stale(session_factory, run.id)
        capturing.clear()

        # First call.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            first = await svc.reclaim_stale_run(run.id)
            await session.commit()

        assert first is True
        first_event_count = len(capturing.emitted)

        # Second call — row is now pending.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            second = await svc.reclaim_stale_run(run.id)
            await session.commit()

        assert second is False
        # No additional events emitted.
        assert len(capturing.emitted) == first_event_count

    async def test_terminal_run_returns_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """reclaim_stale_run on a completed/failed run → False, no side effects."""
        capturing = CapturingEventService()

        # Insert and complete a run.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.create_pipeline_run(
                pipeline_name='terminal_pipe',
                pipeline_version=1,
                args={},
                trigger_source=PipelineTriggerSource.http,
            )
            run = result.run
            await svc.mark_pipeline_running(run.id, worker_id=_WORKER_A)
            await svc.mark_pipeline_completed(run.id)
            await session.commit()

        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            reclaim_result = await svc.reclaim_stale_run(run.id)
            await session.commit()

        assert reclaim_result is False
        assert capturing.emitted == []

    async def test_awaiting_event_run_returns_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """reclaim_stale_run on awaiting_event run → False (guard rejects non-running rows)."""
        capturing = CapturingEventService()
        run = await _insert_running(session_factory, capturing)

        # Transition to awaiting_event.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.mark_pipeline_awaiting_event(run.id)
            await session.commit()

        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.reclaim_stale_run(run.id)
            await session.commit()

        assert result is False
        assert capturing.emitted == []


class TestListStaleRunIds:
    async def test_returns_only_stale_running_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """list_stale_run_ids returns only running rows past threshold, ordered by started_at."""
        capturing = CapturingEventService()

        # Insert two running runs and make one stale.
        run_fresh = await _insert_running(session_factory, capturing, pipeline_name='fresh_pipe')
        run_stale = await _insert_running(session_factory, capturing, pipeline_name='stale_pipe')
        await _make_stale(session_factory, run_stale.id)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            stale_ids = await svc.list_stale_run_ids(limit=50)
            await session.commit()

        assert run_stale.id in stale_ids
        assert run_fresh.id not in stale_ids

    async def test_limit_is_respected(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """list_stale_run_ids respects the limit parameter."""
        capturing = CapturingEventService()

        # Insert 3 stale runs.
        for i in range(3):
            run = await _insert_running(session_factory, capturing, pipeline_name=f'limit_pipe_{i}')
            await _make_stale(session_factory, run.id)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            stale_ids = await svc.list_stale_run_ids(limit=2)
            await session.commit()

        assert len(stale_ids) <= 2
