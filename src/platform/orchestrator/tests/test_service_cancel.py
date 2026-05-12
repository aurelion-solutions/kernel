# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service unit tests for PipelineOrchestratorService.request_cancel (Step 18)."""

from __future__ import annotations

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
from src.platform.orchestrator.service_types import (
    AlreadyCancellingError,
    CancelOutcome,
    OrchestratorRowMissing,
    TerminalStatusError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_svc(session: AsyncSession, capturing: CapturingEventService) -> PipelineOrchestratorService:
    return PipelineOrchestratorService(
        session=session,
        events=EventService(sink=capturing),
        logs=NoOpLogService(),
    )


async def _insert_pending(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
) -> PipelineRun:
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name='cancel_test',
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='cid',
        )
        await session.commit()
    return result.run


async def _transition_to_running(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    run_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        await svc.mark_pipeline_running(run_id, worker_id='worker-1', correlation_id='cid')
        await session.commit()


async def _get_run(
    session_factory: async_sessionmaker[AsyncSession],
    run_id: uuid.UUID,
) -> PipelineRun:
    async with session_factory() as session:
        result = await session.execute(
            sa.select(PipelineRun).where(PipelineRun.id == run_id).execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        assert row is not None
        return row


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRequestCancelPending:
    async def test_returns_cancelled_sync(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run = await _insert_pending(session_factory, capturing)
        capturing.clear()  # discard create event

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            outcome = await svc.request_cancel(run.id, correlation_id='cid-cancel')
            await session.commit()

        assert isinstance(outcome, CancelOutcome)
        assert outcome.status == PipelineRunStatus.cancelled
        assert outcome.sync is True
        assert outcome.run_id == run.id

    async def test_run_row_status_is_cancelled(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run = await _insert_pending(session_factory, capturing)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.request_cancel(run.id, correlation_id='cid-cancel')
            await session.commit()

        refreshed = await _get_run(session_factory, run.id)
        assert refreshed.status == PipelineRunStatus.cancelled
        assert refreshed.finished_at is not None

    async def test_emits_one_cancelled_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run = await _insert_pending(session_factory, capturing)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.request_cancel(run.id, correlation_id='cid-cancel')
            await session.commit()

        events = capturing.filter_by_type('pipeline.run.cancelled')
        assert len(events) == 1
        assert events[0].payload['previous_status'] == 'pending'
        assert events[0].payload['run_id'] == str(run.id)


class TestRequestCancelAwaitingEvent:
    async def _setup_awaiting(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        capturing: CapturingEventService,
    ) -> tuple[uuid.UUID, uuid.UUID]:
        """Insert pending → running → awaiting_event run with a step and waiter."""
        run = await _insert_pending(session_factory, capturing)
        await _transition_to_running(session_factory, capturing, run.id)

        from datetime import UTC, datetime, timedelta

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            step = await svc.create_step_run(run.id, 'step1', {}, correlation_id='cid')
            await svc.mark_step_awaiting_event(step.id, correlation_id='cid')
            await svc.create_pipeline_event_waiter(
                step.id,
                event_type='some.event',
                match={},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await svc.mark_pipeline_awaiting_event(run.id, correlation_id='cid')
            await session.commit()
        return run.id, step.id

    async def test_returns_cancelled_sync(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run_id, _ = await self._setup_awaiting(session_factory, capturing)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            outcome = await svc.request_cancel(run_id, correlation_id='cid-cancel')
            await session.commit()

        assert outcome.status == PipelineRunStatus.cancelled
        assert outcome.sync is True

    async def test_step_run_cancelled_waiter_deleted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run_id, step_id = await self._setup_awaiting(session_factory, capturing)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.request_cancel(run_id, correlation_id='cid-cancel')
            await session.commit()

        # step should be cancelled
        async with session_factory() as session:
            step_result = await session.execute(
                sa.select(StepRun).where(StepRun.id == step_id).execution_options(populate_existing=True)
            )
            step = step_result.scalar_one_or_none()
            assert step is not None
            assert step.status == StepRunStatus.cancelled

        # waiter should be deleted
        async with session_factory() as session:
            waiter_result = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_id)
            )
            assert waiter_result.scalar_one_or_none() is None

    async def test_emits_cancelled_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run_id, _ = await self._setup_awaiting(session_factory, capturing)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.request_cancel(run_id, correlation_id='cid-cancel')
            await session.commit()

        events = capturing.filter_by_type('pipeline.run.cancelled')
        assert len(events) == 1
        assert events[0].payload['previous_status'] == 'awaiting_event'


class TestRequestCancelRunning:
    async def test_returns_cancelling_async(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run = await _insert_pending(session_factory, capturing)
        await _transition_to_running(session_factory, capturing, run.id)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            outcome = await svc.request_cancel(run.id, correlation_id='cid-cancel')
            await session.commit()

        assert outcome.status == PipelineRunStatus.cancelling
        assert outcome.sync is False

    async def test_no_cancelled_event_emitted(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run = await _insert_pending(session_factory, capturing)
        await _transition_to_running(session_factory, capturing, run.id)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.request_cancel(run.id, correlation_id='cid-cancel')
            await session.commit()

        events = capturing.filter_by_type('pipeline.run.cancelled')
        assert len(events) == 0


class TestRequestCancelAlreadyCancelling:
    async def test_raises_already_cancelling(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        run = await _insert_pending(session_factory, capturing)
        await _transition_to_running(session_factory, capturing, run.id)

        # Transition to cancelling.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.request_cancel(run.id, correlation_id='cid')
            await session.commit()

        with pytest.raises(AlreadyCancellingError) as exc_info:
            async with session_factory() as session:
                svc = _make_svc(session, capturing)
                await svc.request_cancel(run.id, correlation_id='cid-2')
                await session.commit()

        assert exc_info.value.run_id == run.id


class TestRequestCancelTerminal:
    @pytest.mark.parametrize(
        'terminal_status',
        [
            PipelineRunStatus.completed,
            PipelineRunStatus.failed,
            PipelineRunStatus.cancelled,
            PipelineRunStatus.failed_timeout,
        ],
    )
    async def test_raises_terminal_status_error(
        self,
        terminal_status: PipelineRunStatus,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        # Insert run directly in the target terminal status.
        async with session_factory() as session:
            run = PipelineRun(
                pipeline_name='cancel_terminal_test',
                pipeline_version=1,
                args={},
                content_hash='abc' + terminal_status.value,
                status=terminal_status,
                trigger_source=PipelineTriggerSource.http,
            )
            session.add(run)
            await session.commit()
            run_id = run.id

        with pytest.raises(TerminalStatusError) as exc_info:
            async with session_factory() as session:
                svc = _make_svc(session, capturing)
                await svc.request_cancel(run_id, correlation_id='cid')
                await session.commit()

        assert exc_info.value.run_id == run_id
        assert exc_info.value.status == terminal_status


class TestRequestCancelNotFound:
    async def test_raises_row_missing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        missing_id = uuid.uuid4()

        with pytest.raises(OrchestratorRowMissing):
            async with session_factory() as session:
                svc = _make_svc(session, capturing)
                await svc.request_cancel(missing_id, correlation_id='cid')
                await session.commit()
