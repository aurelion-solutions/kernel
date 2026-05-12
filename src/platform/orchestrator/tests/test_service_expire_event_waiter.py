# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PipelineOrchestratorService.expire_event_waiter and
list_expired_waiter_step_ids.

All tests use real PostgreSQL (via session_factory from root conftest).
Expiry is forced by setting expires_at in the past via raw SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.models import (
    PipelineEventWaiter,
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

_NOW = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
_PAST = _NOW - timedelta(minutes=5)
_FUTURE = _NOW + timedelta(minutes=5)


def _make_svc(session: AsyncSession, capturing: CapturingEventService) -> PipelineOrchestratorService:
    return PipelineOrchestratorService(
        session=session,
        events=EventService(sink=capturing),
        logs=NoOpLogService(),
    )


async def _insert_awaiting_run(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    *,
    pipeline_name: str = 'expire_pipe',
) -> tuple[PipelineRun, StepRun]:
    """Insert a run + step in awaiting_event state."""
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='test-expire',
        )
        run = result.run
        await svc.mark_pipeline_running(run.id, worker_id='worker-1', correlation_id='test-expire')
        step = await svc.create_step_run(run.id, 'wait_step', {}, correlation_id='test-expire')
        await svc.mark_step_awaiting_event(step.id, correlation_id='test-expire')
        await svc.mark_pipeline_awaiting_event(run.id, correlation_id='test-expire')
        await session.commit()
        return run, step


async def _insert_waiter(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    step_run_id: object,
    *,
    expires_at: datetime,
) -> PipelineEventWaiter:
    """Insert a waiter for the given step_run_id with the given expires_at."""
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        waiter = await svc.create_pipeline_event_waiter(
            step_run_id=step_run_id,  # type: ignore[arg-type]
            event_type='user.approved',
            match={},
            expires_at=expires_at,
        )
        await session.commit()
        return waiter


# ---------------------------------------------------------------------------
# Tests: expire_event_waiter
# ---------------------------------------------------------------------------


class TestExpireEventWaiter:
    async def test_happy_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Expired waiter → step + run both failed_timeout; waiter gone; events emitted."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing)
        await _insert_waiter(session_factory, capturing, step.id, expires_at=_PAST)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok, returned_run_id = await svc.expire_event_waiter(step.id, correlation_id='expire-corr')
            await session.commit()

        assert ok is True
        assert returned_run_id == run.id

        # Waiter must be gone.
        async with session_factory() as session:
            waiter_row = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step.id)
            )
            assert waiter_row.scalar_one_or_none() is None

        # StepRun → failed_timeout.
        async with session_factory() as session:
            step_row = await session.get(StepRun, step.id)
        assert step_row is not None
        assert step_row.status == StepRunStatus.failed_timeout
        assert step_row.error == 'event_timeout'
        assert step_row.finished_at is not None

        # PipelineRun → failed_timeout.
        async with session_factory() as session:
            run_row = await session.get(PipelineRun, run.id)
        assert run_row is not None
        assert run_row.status == PipelineRunStatus.failed_timeout
        assert run_row.error == 'event_timeout'
        assert run_row.finished_at is not None

        # Both events emitted with correct error.
        event_types = [e.event_type for e in capturing.emitted]
        assert 'pipeline.step.failed' in event_types
        assert 'pipeline.run.failed' in event_types
        step_evt = next(e for e in capturing.emitted if e.event_type == 'pipeline.step.failed')
        run_evt = next(e for e in capturing.emitted if e.event_type == 'pipeline.run.failed')
        assert step_evt.payload['error'] == 'event_timeout'
        assert run_evt.payload['error'] == 'event_timeout'

    async def test_idempotent_when_waiter_missing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Second call after waiter already deleted → (False, None), no extra events."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing)
        await _insert_waiter(session_factory, capturing, step.id, expires_at=_PAST)
        capturing.clear()

        # First call.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok1, _ = await svc.expire_event_waiter(step.id)
            await session.commit()

        assert ok1 is True
        first_event_count = len(capturing.emitted)

        # Second call — waiter gone.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok2, run_id2 = await svc.expire_event_waiter(step.id)
            await session.commit()

        assert ok2 is False
        assert run_id2 is None
        assert len(capturing.emitted) == first_event_count

    async def test_no_op_when_run_already_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Run already cancelled → (False, None); waiter deleted (orphan cleanup); no events."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing)
        await _insert_waiter(session_factory, capturing, step.id, expires_at=_PAST)

        # Manually move run to cancelled (simulating cancel won the race).
        async with session_factory() as session:
            await session.execute(
                sa.update(PipelineRun)
                .where(PipelineRun.id == run.id)
                .values(status=PipelineRunStatus.cancelled, finished_at=sa.func.now())
            )
            await session.commit()

        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok, run_id = await svc.expire_event_waiter(step.id)
            await session.commit()

        assert ok is False
        assert run_id is None
        # No events emitted.
        assert capturing.emitted == []

        # Waiter should be deleted (orphan cleanup).
        async with session_factory() as session:
            waiter_row = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step.id)
            )
            assert waiter_row.scalar_one_or_none() is None

    async def test_no_op_when_step_not_awaiting_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Step in running state (race) → (False, None); no events."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing)
        await _insert_waiter(session_factory, capturing, step.id, expires_at=_PAST)

        # Force step back to running (simulate race).
        async with session_factory() as session:
            await session.execute(sa.update(StepRun).where(StepRun.id == step.id).values(status=StepRunStatus.running))
            await session.commit()

        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok, run_id = await svc.expire_event_waiter(step.id)
            await session.commit()

        assert ok is False
        assert run_id is None
        assert capturing.emitted == []

        # Waiter deleted (orphan cleanup).
        async with session_factory() as session:
            waiter_row = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step.id)
            )
            assert waiter_row.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# Tests: list_expired_waiter_step_ids
# ---------------------------------------------------------------------------


class TestListExpiredWaiterStepIds:
    async def test_returns_only_expired(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """3 expired + 1 fresh → list length 3, ordered expires_at ASC."""
        capturing = CapturingEventService()

        # Insert 3 expired.
        step_ids = []
        for i in range(3):
            run, step = await _insert_awaiting_run(session_factory, capturing, pipeline_name=f'exp_pipe_{i}')
            # Stagger expires_at to verify ordering.
            await _insert_waiter(
                session_factory,
                capturing,
                step.id,
                expires_at=_PAST - timedelta(minutes=i),
            )
            step_ids.append((step.id, _PAST - timedelta(minutes=i)))

        # Insert 1 fresh.
        run_fresh, step_fresh = await _insert_awaiting_run(session_factory, capturing, pipeline_name='fresh_pipe')
        await _insert_waiter(session_factory, capturing, step_fresh.id, expires_at=_FUTURE)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ids = await svc.list_expired_waiter_step_ids(_NOW)
            await session.commit()

        assert len(ids) == 3
        assert step_fresh.id not in ids

        # Verify ordering: oldest expires_at first.
        # step_ids sorted: index 2 (-7m), 1 (-6m), 0 (-5m)
        expected_order = [s for s, _ in sorted(step_ids, key=lambda x: x[1])]
        assert ids == expected_order

    async def test_limit_truncates(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """limit=2 with 3 expired → only 2 returned."""
        capturing = CapturingEventService()

        for i in range(3):
            run, step = await _insert_awaiting_run(session_factory, capturing, pipeline_name=f'lim_pipe_{i}')
            await _insert_waiter(session_factory, capturing, step.id, expires_at=_PAST)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ids = await svc.list_expired_waiter_step_ids(_NOW, limit=2)
            await session.commit()

        assert len(ids) == 2
