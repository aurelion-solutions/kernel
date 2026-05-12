# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service unit tests for PipelineOrchestratorService.create_retry (Step 19)."""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
)
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.orchestrator.service_types import (
    OrchestratorRowMissing,
    RunNotRetryableError,
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


async def _insert_run_with_status(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    status: PipelineRunStatus,
    *,
    pipeline_name: str = 'retry_test',
    args: dict | None = None,
) -> PipelineRun:
    """Insert a PipelineRun directly with the given status (bypasses guards)."""
    import hashlib
    import json

    resolved_args = args or {}
    canonical = json.dumps(resolved_args, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()
    # make unique per call + status to avoid UNIQUE violations between tests
    import uuid as _uuid

    content_hash = hashlib.sha256((content_hash + status.value + str(_uuid.uuid4())).encode()).hexdigest()

    async with session_factory() as session:
        run = PipelineRun(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args=resolved_args,
            content_hash=content_hash,
            status=status,
            trigger_source=PipelineTriggerSource.http,
        )
        session.add(run)
        await session.commit()
        # refresh to get id
        await session.refresh(run)
        return run


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
# Tests — happy path (terminal sources)
# ---------------------------------------------------------------------------


class TestCreateRetryTerminalSources:
    async def test_completed_source_inserts_fresh_row_and_emits_created(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.completed)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.create_retry(source.id, correlation_id='cid-retry')
            await session.commit()

        assert result.created is True
        assert result.run.retry_of_run_id == source.id
        assert result.run.status == PipelineRunStatus.pending
        assert result.run.trigger_source == PipelineTriggerSource.retry
        assert result.run.pipeline_name == source.pipeline_name
        assert result.run.pipeline_version == source.pipeline_version

        created_events = capturing.filter_by_type('pipeline.run.created')
        assert len(created_events) == 1
        assert created_events[0].payload['run_id'] == str(result.run.id)

    async def test_failed_source_inserts_fresh_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.failed)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.create_retry(source.id)
            await session.commit()

        assert result.run.retry_of_run_id == source.id
        assert result.run.status == PipelineRunStatus.pending

    async def test_cancelled_source_inserts_fresh_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.cancelled)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.create_retry(source.id)
            await session.commit()

        assert result.run.retry_of_run_id == source.id

    async def test_failed_timeout_source_inserts_fresh_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.failed_timeout)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.create_retry(source.id)
            await session.commit()

        assert result.run.retry_of_run_id == source.id


# ---------------------------------------------------------------------------
# Tests — non-retryable (guard rejects)
# ---------------------------------------------------------------------------


class TestCreateRetryNonRetryable:
    async def test_pending_raises_non_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.pending)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            with pytest.raises(RunNotRetryableError) as exc_info:
                await svc.create_retry(source.id)

        assert exc_info.value.reason == 'non_terminal'
        assert exc_info.value.status == PipelineRunStatus.pending

    async def test_running_raises_non_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.running)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            with pytest.raises(RunNotRetryableError) as exc_info:
                await svc.create_retry(source.id)

        assert exc_info.value.reason == 'non_terminal'

    async def test_awaiting_event_raises_non_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.awaiting_event)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            with pytest.raises(RunNotRetryableError) as exc_info:
                await svc.create_retry(source.id)

        assert exc_info.value.reason == 'non_terminal'

    async def test_cancelling_raises_cancelling_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.cancelling)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            with pytest.raises(RunNotRetryableError) as exc_info:
                await svc.create_retry(source.id)

        assert exc_info.value.reason == 'cancelling'
        assert exc_info.value.status == PipelineRunStatus.cancelling

    async def test_unknown_id_raises_row_missing(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        unknown_id = uuid.uuid4()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            with pytest.raises(OrchestratorRowMissing):
                await svc.create_retry(unknown_id)


# ---------------------------------------------------------------------------
# Tests — event emission and chained retries
# ---------------------------------------------------------------------------


class TestCreateRetryEvents:
    async def test_emits_pipeline_run_created_exactly_once_with_retry_of_payload(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.completed)
        capturing.clear()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            result = await svc.create_retry(source.id, correlation_id='emit-cid')
            await session.commit()

        created_events = capturing.filter_by_type('pipeline.run.created')
        assert len(created_events) == 1
        envelope = created_events[0]
        assert envelope.payload['run_id'] == str(result.run.id)
        assert envelope.correlation_id == 'emit-cid'

    async def test_retry_of_a_retry_succeeds_when_first_retry_terminal(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A retry of a completed retry is itself allowed (chained retries)."""
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.completed)
        capturing.clear()

        # First retry
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            first_retry = await svc.create_retry(source.id)
            await session.commit()

        # Move first retry to completed directly in DB
        async with session_factory() as session:
            await session.execute(
                sa.update(PipelineRun)
                .where(PipelineRun.id == first_retry.run.id)
                .values(status=PipelineRunStatus.completed)
                .execution_options(synchronize_session=False)
            )
            await session.commit()

        # Second retry of first retry
        capturing.clear()
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            second_retry = await svc.create_retry(first_retry.run.id)
            await session.commit()

        assert second_retry.run.retry_of_run_id == first_retry.run.id
        assert second_retry.run.status == PipelineRunStatus.pending

    async def test_two_concurrent_retries_of_same_terminal_source_both_succeed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Concurrent retries of the same terminal source both insert successfully.

        The partial UNIQUE excludes rows where retry_of_run_id IS NOT NULL, so
        multiple concurrent retries are always allowed by design.
        Uses two independent sessions to bypass session-level identity map.
        """
        capturing = CapturingEventService()
        source = await _insert_run_with_status(session_factory, capturing, PipelineRunStatus.completed)
        capturing.clear()

        async with session_factory() as session1, session_factory() as session2:
            svc1 = _make_svc(session1, capturing)
            svc2 = _make_svc(session2, capturing)

            result1 = await svc1.create_retry(source.id)
            result2 = await svc2.create_retry(source.id)

            await session1.commit()
            await session2.commit()

        assert result1.run.id != result2.run.id
        assert result1.run.retry_of_run_id == source.id
        assert result2.run.retry_of_run_id == source.id
