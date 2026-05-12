# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Fixtures for orchestrator service tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus, PipelineTriggerSource
from src.platform.orchestrator.service import PipelineOrchestratorService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    """EventService backed by CapturingEventService (acts as a sink)."""
    return EventService(sink=capturing_events)


@pytest_asyncio.fixture
async def orchestrator_service(
    session_factory: async_sessionmaker[AsyncSession],
    event_service: EventService,
) -> AsyncGenerator[PipelineOrchestratorService]:
    """Yield a PipelineOrchestratorService with a committed session."""
    async with session_factory() as session:
        svc = PipelineOrchestratorService(
            session=session,
            events=event_service,
            logs=NoOpLogService(),
        )
        yield svc
        await session.commit()


@pytest_asyncio.fixture
async def pending_run(
    session_factory: async_sessionmaker[AsyncSession],
    event_service: EventService,
) -> PipelineRun:
    """Insert a pending PipelineRun and return it."""
    async with session_factory() as session:
        svc = PipelineOrchestratorService(
            session=session,
            events=event_service,
            logs=NoOpLogService(),
        )
        result = await svc.create_pipeline_run(
            pipeline_name='test_pipeline',
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='test-corr',
        )
        await session.commit()
        return result.run


@pytest_asyncio.fixture
async def running_run(
    session_factory: async_sessionmaker[AsyncSession],
    event_service: EventService,
    pending_run: PipelineRun,
) -> PipelineRun:
    """Transition pending_run to running and return the refreshed row."""
    async with session_factory() as session:
        svc = PipelineOrchestratorService(
            session=session,
            events=event_service,
            logs=NoOpLogService(),
        )
        await svc.mark_pipeline_running(pending_run.id, worker_id='worker-1', correlation_id='test-corr')
        await session.commit()
        row = await session.get(PipelineRun, pending_run.id)
        assert row is not None
        assert row.status == PipelineRunStatus.running
        return row
