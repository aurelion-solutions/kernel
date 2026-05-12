# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PipelineOrchestratorService.resolve_pipeline_event_waiter and
find_matching_waiter_step_ids.

All tests use real PostgreSQL (via session_factory from root conftest).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
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
from src.platform.orchestrator.service_types import OrchestratorRowMissing

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FUTURE = datetime.now(UTC) + timedelta(minutes=10)


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
    pipeline_name: str = 'waiter_pipe',
) -> tuple[PipelineRun, StepRun]:
    """Insert a run + step both in awaiting_event status, then insert a waiter row."""
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='test-resolve',
        )
        run = result.run
        await svc.mark_pipeline_running(run.id, worker_id='w1', correlation_id='test-resolve')
        step = await svc.create_step_run(run.id, 'wait_step', {}, correlation_id='test-resolve')
        await svc.mark_step_awaiting_event(step.id, correlation_id='test-resolve')
        await svc.mark_pipeline_awaiting_event(run.id, correlation_id='test-resolve')
        await session.commit()
        return run, step


async def _insert_waiter(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    step_run_id: object,
    *,
    event_type: str = 'approval.granted',
    match: dict | None = None,
) -> PipelineEventWaiter:
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        waiter = await svc.create_pipeline_event_waiter(
            step_run_id=step_run_id,  # type: ignore[arg-type]
            event_type=event_type,
            match=match or {},
            expires_at=_FUTURE,
        )
        await session.commit()
        return waiter


# ---------------------------------------------------------------------------
# resolve_pipeline_event_waiter
# ---------------------------------------------------------------------------


class TestResolveEventWaiter:
    async def test_happy_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Happy path: waiter deleted, step completed, run → pending, event emitted."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing)
        await _insert_waiter(session_factory, capturing, step.id)
        capturing.clear()

        payload = {'request_id': 'r1', 'approver': 'alice'}

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            resolved = await svc.resolve_pipeline_event_waiter(step.id, payload, correlation_id='corr-1')
            await session.commit()

        assert resolved is True

        # Waiter must be gone.
        async with session_factory() as session:
            row = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step.id)
            )
            assert row.scalar_one_or_none() is None

        # Step → completed with result = payload.
        async with session_factory() as session:
            step_row = await session.get(StepRun, step.id)
        assert step_row is not None
        assert step_row.status == StepRunStatus.completed
        assert step_row.result == payload

        # Run → pending (not running — runner picks up from pending queue).
        async with session_factory() as session:
            run_row = await session.get(PipelineRun, run.id)
        assert run_row is not None
        assert run_row.status == PipelineRunStatus.pending

        # Event emitted.
        assert any(e.event_type == 'pipeline.step.completed' for e in capturing.emitted)

    async def test_waiter_missing_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No waiter for the step_run_id → OrchestratorRowMissing."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing)
        # Do NOT insert waiter.

        import uuid  # noqa: PLC0415

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            with pytest.raises(OrchestratorRowMissing):
                await svc.resolve_pipeline_event_waiter(uuid.uuid4(), {})

    async def test_step_missing_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Waiter found but StepRun missing → OrchestratorRowMissing.

        We temporarily disable FK checks (session_replication_role=replica) so we
        can insert a waiter pointing to a non-existent step_run_id without the DB
        rejecting it.  This is test-only; production code never violates FK order.
        """
        import uuid  # noqa: PLC0415

        fake_step_id = uuid.uuid4()
        capturing = CapturingEventService()

        # Insert the waiter via raw SQL bypassing FK constraint (test only).
        async with session_factory() as session:
            await session.execute(sa.text("SET session_replication_role = 'replica'"))
            await session.execute(
                sa.text(
                    'INSERT INTO pipeline_event_waiters (step_run_id, event_type, match, expires_at) '
                    "VALUES (:sid, 'fakeevt', '{}', :exp)"
                ),
                {'sid': fake_step_id, 'exp': _FUTURE},
            )
            await session.execute(sa.text("SET session_replication_role = 'origin'"))
            await session.commit()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            with pytest.raises(OrchestratorRowMissing):
                await svc.resolve_pipeline_event_waiter(fake_step_id, {})

    async def test_run_already_cancelled_step_still_completes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Run in cancelling state: step completes, run stays in cancelling (guarded UPDATE → 0 rows)."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing)
        await _insert_waiter(session_factory, capturing, step.id)

        # Transition run to cancelling.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.mark_pipeline_cancelling(run.id, correlation_id='cancel')
            await session.commit()

        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            # resolve should NOT raise — it just skips the guarded run update.
            resolved = await svc.resolve_pipeline_event_waiter(step.id, {'foo': 'bar'}, correlation_id='corr')
            await session.commit()

        assert resolved is True

        # Step completed.
        async with session_factory() as session:
            step_row = await session.get(StepRun, step.id)
        assert step_row is not None
        assert step_row.status == StepRunStatus.completed

        # Run stays in cancelling (not pending).
        async with session_factory() as session:
            run_row = await session.get(PipelineRun, run.id)
        assert run_row is not None
        assert run_row.status == PipelineRunStatus.cancelling

    async def test_event_emission_failure_propagates(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """EventService.emit failure → re-raises (service does not swallow)."""
        from src.platform.events.service import EventService as _ES  # noqa: PLC0415

        class _BoomSink:
            async def emit(self, event: object) -> None:
                raise RuntimeError('event sink failure')

        run, step = await _insert_awaiting_run(session_factory, CapturingEventService(), pipeline_name='fail_emit_pipe')
        await _insert_waiter(session_factory, CapturingEventService(), step.id, event_type='test.event')

        async with session_factory() as session:
            svc = PipelineOrchestratorService(
                session=session,
                events=_ES(sink=_BoomSink()),  # type: ignore[arg-type]
                logs=NoOpLogService(),
            )
            with pytest.raises(RuntimeError, match='event sink failure'):
                await svc.resolve_pipeline_event_waiter(step.id, {})


# ---------------------------------------------------------------------------
# find_matching_waiter_step_ids
# ---------------------------------------------------------------------------


class TestFindMatchingWaiterStepIds:
    async def test_exact_key_match(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Waiter match is a subset of payload → returned."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing, pipeline_name='p_exact')
        await _insert_waiter(
            session_factory,
            capturing,
            step.id,
            event_type='approval.granted',
            match={'request_id': 'r1'},
        )

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ids = await svc.find_matching_waiter_step_ids(
                'approval.granted',
                {'request_id': 'r1', 'approver': 'alice'},
            )
        assert step.id in ids

    async def test_empty_match_always_matches(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Empty match {} is contained in any payload."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing, pipeline_name='p_empty')
        await _insert_waiter(
            session_factory,
            capturing,
            step.id,
            event_type='any.event',
            match={},
        )

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ids = await svc.find_matching_waiter_step_ids(
                'any.event',
                {'foo': 'bar', 'extra': 123},
            )
        assert step.id in ids

    async def test_containment_direction(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """match <@ payload: match is contained in payload (extra payload keys OK)."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing, pipeline_name='p_contain')
        await _insert_waiter(
            session_factory,
            capturing,
            step.id,
            event_type='foo.bar',
            match={'key': 'value'},
        )

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            # Extra key in payload is fine.
            ids = await svc.find_matching_waiter_step_ids(
                'foo.bar',
                {'key': 'value', 'extra': 'ignored'},
            )
        assert step.id in ids

    async def test_matched_status_excluded(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Waiters in 'matched' status must NOT be returned."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing, pipeline_name='p_matched')
        await _insert_waiter(
            session_factory,
            capturing,
            step.id,
            event_type='x.event',
            match={},
        )

        # Force status to 'matched' via raw SQL.
        async with session_factory() as session:
            await session.execute(
                sa.text("UPDATE pipeline_event_waiters SET status = 'matched' WHERE step_run_id = :sid"),
                {'sid': step.id},
            )
            await session.commit()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ids = await svc.find_matching_waiter_step_ids('x.event', {})
        assert step.id not in ids

    async def test_wrong_event_type_not_returned(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Waiter event_type mismatch → empty list."""
        capturing = CapturingEventService()
        run, step = await _insert_awaiting_run(session_factory, capturing, pipeline_name='p_type_miss')
        await _insert_waiter(
            session_factory,
            capturing,
            step.id,
            event_type='specific.event',
            match={},
        )

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ids = await svc.find_matching_waiter_step_ids('other.event', {})
        assert step.id not in ids


# ---------------------------------------------------------------------------
# Concurrent resolve — race safety
# ---------------------------------------------------------------------------


class TestConcurrentResolve:
    async def test_concurrent_resolve_only_one_wins(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Two concurrent resolve attempts on the same waiter: exactly one wins.

        The winner returns True and emits pipeline.step.completed.
        The loser raises OrchestratorRowMissing (FOR UPDATE blocks, then re-reads
        a deleted row) and emits nothing.

        We simulate concurrency sequentially:
        1. Open session A, SELECT waiter FOR UPDATE — holds the lock.
        2. Open session B, attempt resolve — it will block on the lock.
           We simulate this by committing session A first and then running
           session B; session B finds no waiter row → raises the expected error.
        """

        capturing_a = CapturingEventService()
        capturing_b = CapturingEventService()

        run, step = await _insert_awaiting_run(session_factory, capturing_a, pipeline_name='p_concurrent')
        await _insert_waiter(session_factory, capturing_a, step.id, event_type='concurrent.event')
        capturing_a.clear()

        payload = {'x': 1}

        async def _attempt(capturing: CapturingEventService) -> bool:
            async with session_factory() as session:
                svc = _make_svc(session, capturing)
                resolved = await svc.resolve_pipeline_event_waiter(step.id, payload)
                await session.commit()
                return resolved

        # First call wins.
        result_a = await _attempt(capturing_a)
        assert result_a is True
        assert any(e.event_type == 'pipeline.step.completed' for e in capturing_a.emitted)

        # Second call — waiter row is gone after first commit → OrchestratorRowMissing.
        with pytest.raises(OrchestratorRowMissing):
            await _attempt(capturing_b)

        # No event emitted by the losing attempt.
        assert not any(e.event_type == 'pipeline.step.completed' for e in capturing_b.emitted)
