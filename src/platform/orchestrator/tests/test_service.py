# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service unit tests for PipelineOrchestratorService.

All tests use real Postgres (via session_factory fixture from root conftest).
Events are captured via CapturingEventService / EventService.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

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
from src.platform.orchestrator.service_types import OrchestratorRowMissing, OrchestratorStateConflict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_svc(session: AsyncSession, capturing: CapturingEventService) -> PipelineOrchestratorService:
    return PipelineOrchestratorService(
        session=session,
        events=EventService(sink=capturing),
        logs=NoOpLogService(),
    )


async def _get_run(session: AsyncSession, run_id: uuid.UUID) -> PipelineRun:
    """Fetch a PipelineRun from DB, always loading fresh state from the database."""
    result = await session.execute(
        sa.select(PipelineRun).where(PipelineRun.id == run_id).execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    assert row is not None
    return row


async def _get_step(session: AsyncSession, step_id: uuid.UUID) -> StepRun:
    """Fetch a StepRun from DB, always loading fresh state from the database."""
    result = await session.execute(
        sa.select(StepRun).where(StepRun.id == step_id).execution_options(populate_existing=True)
    )
    row = result.scalar_one_or_none()
    assert row is not None
    return row


# ---------------------------------------------------------------------------
# Section 1 — create_pipeline_run
# ---------------------------------------------------------------------------


class TestCreatePipelineRun:
    async def test_happy_path_row_exists_and_event_emitted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.create_pipeline_run(
                pipeline_name='pipe_a',
                pipeline_version=1,
                args={'x': 1},
                trigger_source=PipelineTriggerSource.http,
                correlation_id='cid-1',
            )
            await session.commit()

        assert result.created is True
        assert result.run.status == PipelineRunStatus.pending
        assert result.run.worker_id is None
        assert result.run.current_step is None

        events = capturing.filter_by_type('pipeline.run.created')
        assert len(events) == 1
        assert events[0].payload['run_id'] == str(result.run.id)

    async def test_idempotent_dedupe_same_id_no_second_emit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            r1 = await svc.create_pipeline_run(
                pipeline_name='pipe_b',
                pipeline_version=1,
                args={'k': 'v'},
                trigger_source=PipelineTriggerSource.http,
                correlation_id='cid-2',
            )
            r2 = await svc.create_pipeline_run(
                pipeline_name='pipe_b',
                pipeline_version=1,
                args={'k': 'v'},
                trigger_source=PipelineTriggerSource.http,
                correlation_id='cid-2',
            )
            await session.commit()

        assert r1.created is True
        assert r2.created is False
        assert r1.run.id == r2.run.id
        # Only one event emitted on the first call.
        assert len(capturing.filter_by_type('pipeline.run.created')) == 1

    async def test_create_after_terminal_inserts_new_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        # Insert and complete a run.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            r1 = await svc.create_pipeline_run(
                pipeline_name='pipe_c',
                pipeline_version=1,
                args={'x': 42},
                trigger_source=PipelineTriggerSource.http,
                correlation_id='cid-3',
            )
            # Manually force to completed so UNIQUE partial index releases it.
            await session.execute(
                sa.update(PipelineRun)
                .where(PipelineRun.id == r1.run.id)
                .values(status=PipelineRunStatus.completed, finished_at=sa.func.now())
            )
            await session.commit()

        capturing.clear()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            r2 = await svc.create_pipeline_run(
                pipeline_name='pipe_c',
                pipeline_version=1,
                args={'x': 42},
                trigger_source=PipelineTriggerSource.http,
                correlation_id='cid-3b',
            )
            await session.commit()

        assert r2.created is True
        assert r2.run.id != r1.run.id
        assert len(capturing.filter_by_type('pipeline.run.created')) == 1

    async def test_retry_row_bypasses_unique_index(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            original = await svc.create_pipeline_run(
                pipeline_name='pipe_d',
                pipeline_version=1,
                args={'y': 99},
                trigger_source=PipelineTriggerSource.http,
                correlation_id='cid-4',
            )
            # Retry with same args while original is still in-flight.
            retry = await svc.create_pipeline_run(
                pipeline_name='pipe_d',
                pipeline_version=1,
                args={'y': 99},
                trigger_source=PipelineTriggerSource.retry,
                retry_of_run_id=original.run.id,
                correlation_id='cid-4r',
            )
            await session.commit()

        assert retry.created is True
        assert retry.run.id != original.run.id
        assert retry.run.retry_of_run_id == original.run.id


# ---------------------------------------------------------------------------
# Section 2 — PipelineRun status transitions
# ---------------------------------------------------------------------------


class TestPipelineRunTransitions:
    async def test_pending_to_running(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pending_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.mark_pipeline_running(pending_run.id, worker_id='w-1', correlation_id='cid')
            await session.commit()
            row = await _get_run(session, pending_run.id)

        assert row.status == PipelineRunStatus.running
        assert row.worker_id == 'w-1'
        started = capturing.filter_by_type('pipeline.run.started')
        assert len(started) == 1

    async def test_running_to_awaiting_event_clears_worker(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            await svc.mark_pipeline_awaiting_event(running_run.id)
            await session.commit()
            row = await _get_run(session, running_run.id)

        assert row.status == PipelineRunStatus.awaiting_event
        assert row.worker_id is None
        assert row.last_heartbeat_at is None

    async def test_awaiting_event_to_running(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            await svc.mark_pipeline_awaiting_event(running_run.id)
            await svc.mark_pipeline_running_from_awaiting(running_run.id, worker_id='w-2')
            await session.commit()
            row = await _get_run(session, running_run.id)

        assert row.status == PipelineRunStatus.running
        assert row.worker_id == 'w-2'

    async def test_running_to_completed_emits_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.mark_pipeline_completed(running_run.id, correlation_id='cid')
            await session.commit()
            row = await _get_run(session, running_run.id)

        assert row.status == PipelineRunStatus.completed
        assert row.finished_at is not None
        assert len(capturing.filter_by_type('pipeline.run.completed')) == 1

    async def test_running_to_failed_emits_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.mark_pipeline_failed(running_run.id, error='boom', correlation_id='cid')
            await session.commit()
            row = await _get_run(session, running_run.id)

        assert row.status == PipelineRunStatus.failed
        assert row.error == 'boom'
        failed_events = capturing.filter_by_type('pipeline.run.failed')
        assert len(failed_events) == 1
        assert failed_events[0].payload['error'] == 'boom'

    async def test_pending_to_cancelling(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pending_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            await svc.mark_pipeline_cancelling(pending_run.id)
            await session.commit()
            row = await _get_run(session, pending_run.id)

        assert row.status == PipelineRunStatus.cancelling

    async def test_running_to_cancelling(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            await svc.mark_pipeline_cancelling(running_run.id)
            await session.commit()
            row = await _get_run(session, running_run.id)

        assert row.status == PipelineRunStatus.cancelling

    async def test_cancelling_to_cancelled(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            await svc.mark_pipeline_cancelling(running_run.id)
            await svc.mark_pipeline_cancelled(running_run.id)
            await session.commit()
            row = await _get_run(session, running_run.id)

        assert row.status == PipelineRunStatus.cancelled
        assert row.finished_at is not None


# ---------------------------------------------------------------------------
# Section 3 — cancel-vs-complete race
# ---------------------------------------------------------------------------


class TestCancelVsCompleteRace:
    async def test_complete_while_cancelling_collapses_to_cancelled_no_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        """mark_pipeline_completed on a cancelling row → cancelled, no completed event."""
        capturing = CapturingEventService()
        async with session_factory() as session:
            # Force the run into 'cancelling' directly.
            await session.execute(
                sa.update(PipelineRun)
                .where(PipelineRun.id == running_run.id)
                .values(status=PipelineRunStatus.cancelling)
            )
            await session.flush()
            svc = _make_svc(session, capturing)
            # Now call mark_pipeline_completed — should hit the race branch.
            await svc.mark_pipeline_completed(running_run.id)
            await session.commit()
            row = await _get_run(session, running_run.id)

        assert row.status == PipelineRunStatus.cancelled
        assert row.finished_at is not None
        # No pipeline.run.completed emitted.
        assert len(capturing.filter_by_type('pipeline.run.completed')) == 0


# ---------------------------------------------------------------------------
# Section 4 — wrong-state guards raise OrchestratorStateConflict
# ---------------------------------------------------------------------------


class TestStateGuards:
    async def test_mark_running_wrong_state_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            with pytest.raises(OrchestratorStateConflict):
                # Already running — expected pending.
                await svc.mark_pipeline_running(running_run.id, worker_id='w')

    async def test_mark_completed_wrong_state_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pending_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            with pytest.raises(OrchestratorStateConflict):
                # Pending is not running/awaiting_event.
                await svc.mark_pipeline_completed(pending_run.id)


# ---------------------------------------------------------------------------
# Section 5 — StepRun lifecycle
# ---------------------------------------------------------------------------


class TestStepRunLifecycle:
    async def test_create_step_sets_current_step_and_emits_started(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            step = await svc.create_step_run(running_run.id, 'step_one', {'p': 1})
            await session.commit()
            run = await _get_run(session, running_run.id)

        assert run.current_step == 'step_one'
        assert step.status == StepRunStatus.running
        events = capturing.filter_by_type('pipeline.step.started')
        assert len(events) == 1
        assert events[0].payload['step_name'] == 'step_one'

    async def test_mark_step_succeeded_emits_completed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            step = await svc.create_step_run(running_run.id, 'step_two', {})
            await svc.mark_step_succeeded(step.id, result={'out': 'ok'})
            await session.commit()
            s = await _get_step(session, step.id)

        assert s.status == StepRunStatus.completed
        assert s.result == {'out': 'ok'}
        events = capturing.filter_by_type('pipeline.step.completed')
        assert len(events) == 1
        assert events[0].payload['result'] == {'out': 'ok'}

    async def test_mark_step_failed_emits_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            step = await svc.create_step_run(running_run.id, 'step_three', {})
            await svc.mark_step_failed(step.id, error='timeout')
            await session.commit()
            s = await _get_step(session, step.id)

        assert s.status == StepRunStatus.failed
        assert s.error == 'timeout'
        events = capturing.filter_by_type('pipeline.step.failed')
        assert len(events) == 1
        assert events[0].payload['error'] == 'timeout'


# ---------------------------------------------------------------------------
# Section 6 — reclaim_step
# ---------------------------------------------------------------------------


class TestReclaimStep:
    async def test_reclaim_aborts_prev_and_inserts_new_attempt(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            step = await svc.create_step_run(running_run.id, 'step_r', {'a': 1})
            prev_id = step.id
            result = await svc.reclaim_step(running_run.id, 'step_r')
            await session.commit()

            prev = await _get_step(session, prev_id)
            new = await _get_step(session, result.new_step_run_id)

        assert prev.status == StepRunStatus.aborted
        assert prev.error == 'reclaimed: heartbeat lost'
        assert new.status == StepRunStatus.running
        assert new.attempt == 2
        assert result.new_attempt == 2
        assert result.aborted_step_run_id == prev_id
        # No events from reclaim itself.
        assert len(capturing.emitted) == 1  # Only step.started from create_step_run.

    async def test_reclaim_missing_step_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            with pytest.raises(OrchestratorRowMissing):
                await svc.reclaim_step(running_run.id, 'nonexistent_step')


# ---------------------------------------------------------------------------
# Section 7 — PipelineEventWaiter lifecycle
# ---------------------------------------------------------------------------


class TestPipelineEventWaiter:
    async def test_create_waiter_inserts_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            step = await svc.create_step_run(running_run.id, 'w_step', {})
            expires = datetime.now(UTC) + timedelta(minutes=5)
            waiter = await svc.create_pipeline_event_waiter(
                step.id, 'connector.result.received', {'type': 'foo'}, expires
            )
            await session.commit()

        assert waiter.step_run_id == step.id
        assert waiter.event_type == 'connector.result.received'

    async def test_duplicate_waiter_raises_conflict(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            step = await svc.create_step_run(running_run.id, 'w_step2', {})
            expires = datetime.now(UTC) + timedelta(minutes=5)
            await svc.create_pipeline_event_waiter(step.id, 'some.event.type', {}, expires)
            with pytest.raises(OrchestratorStateConflict):
                await svc.create_pipeline_event_waiter(step.id, 'some.event.type', {}, expires)

    async def test_resolve_waiter_completes_step_and_reactivates_run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        capturing = CapturingEventService()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            step = await svc.create_step_run(running_run.id, 'w_step3', {})
            # Park the run.
            await svc.mark_step_awaiting_event(step.id)
            await svc.mark_pipeline_awaiting_event(running_run.id)
            expires = datetime.now(UTC) + timedelta(minutes=5)
            await svc.create_pipeline_event_waiter(step.id, 'evt.type.ok', {}, expires)
            # Resolve.
            payload = {'data': 'resolved'}
            await svc.resolve_pipeline_event_waiter(step.id, payload)
            await session.commit()

            s = await _get_step(session, step.id)
            run = await _get_run(session, running_run.id)
            # Waiter should be deleted.
            waiter_q = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step.id)
            )
            waiter_row = waiter_q.scalar_one_or_none()

        assert s.status == StepRunStatus.completed
        assert s.result == payload
        # awaiting_event → pending (runner picks up from the pending queue).
        assert run.status == PipelineRunStatus.pending
        assert waiter_row is None
        completed_events = capturing.filter_by_type('pipeline.step.completed')
        # Two: one from mark_step_awaiting_event? No — that one doesn't emit.
        # One from create_step_run (step.started) + one from resolve (step.completed).
        assert len(completed_events) == 1

    async def test_resolve_missing_waiter_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        running_run: PipelineRun,
    ) -> None:
        async with session_factory() as session:
            svc = _make_svc(session, CapturingEventService())
            with pytest.raises(OrchestratorRowMissing):
                await svc.resolve_pipeline_event_waiter(uuid.uuid4(), {})
