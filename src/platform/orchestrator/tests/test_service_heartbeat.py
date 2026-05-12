# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PipelineOrchestratorService.refresh_heartbeat.

All tests use real PostgreSQL (via session_factory fixture from root conftest).
Events are captured via CapturingEventService.
"""

from __future__ import annotations

import asyncio

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_A = 'host-1-0'
_WORKER_B = 'host-2-0'


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
            pipeline_name='hb_test_pipe',
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='hb-test',
        )
        await session.commit()
        return result.run


async def _claim_run(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    worker_id: str = _WORKER_A,
) -> PipelineRun:
    """Insert + claim a run, returning the running row."""
    await _insert_pending(session_factory, capturing)
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        claimed = await svc.claim_pending_run(worker_id)
        await session.commit()
    assert claimed is not None
    return claimed


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRefreshHeartbeat:
    async def test_refresh_heartbeat_updates_timestamp(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """claim → sleep 100ms → refresh → last_heartbeat_at strictly greater."""
        capturing = CapturingEventService()
        claimed = await _claim_run(session_factory, capturing)

        # Read initial heartbeat.
        async with session_factory() as session:
            row = await session.get(PipelineRun, claimed.id)
        assert row is not None
        initial_hb = row.last_heartbeat_at
        assert initial_hb is not None

        await asyncio.sleep(0.1)

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok = await svc.refresh_heartbeat(claimed.id, _WORKER_A)
            await session.commit()

        assert ok is True

        # Re-read and compare.
        async with session_factory() as session:
            refreshed = await session.execute(
                sa.select(PipelineRun).where(PipelineRun.id == claimed.id).execution_options(populate_existing=True)
            )
            row_after = refreshed.scalar_one()

        assert row_after.last_heartbeat_at is not None
        assert row_after.last_heartbeat_at > initial_hb

    async def test_refresh_heartbeat_returns_false_on_wrong_worker_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """claim with worker A, refresh with worker B → False, row unchanged."""
        capturing = CapturingEventService()
        claimed = await _claim_run(session_factory, capturing, worker_id=_WORKER_A)

        async with session_factory() as session:
            row = await session.get(PipelineRun, claimed.id)
        assert row is not None
        initial_hb = row.last_heartbeat_at

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok = await svc.refresh_heartbeat(claimed.id, _WORKER_B)
            await session.commit()

        assert ok is False

        # Heartbeat must not have changed.
        async with session_factory() as session:
            row_after = await session.execute(
                sa.select(PipelineRun).where(PipelineRun.id == claimed.id).execution_options(populate_existing=True)
            )
            unchanged = row_after.scalar_one()

        assert unchanged.last_heartbeat_at == initial_hb

    async def test_refresh_heartbeat_returns_false_on_terminal_status(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """completed run → refresh returns False."""
        capturing = CapturingEventService()
        claimed = await _claim_run(session_factory, capturing)

        # Complete the run.
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.mark_pipeline_completed(claimed.id)
            await session.commit()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok = await svc.refresh_heartbeat(claimed.id, _WORKER_A)
            await session.commit()

        assert ok is False

        # Status must still be completed.
        async with session_factory() as session:
            row = await session.execute(
                sa.select(PipelineRun).where(PipelineRun.id == claimed.id).execution_options(populate_existing=True)
            )
            final = row.scalar_one()
        assert final.status == PipelineRunStatus.completed

    async def test_refresh_heartbeat_returns_false_on_awaiting_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """awaiting_event run → refresh returns False (worker_id already cleared)."""
        capturing = CapturingEventService()
        claimed = await _claim_run(session_factory, capturing)

        # Transition to awaiting_event via the service (clears worker_id).
        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            await svc.mark_pipeline_awaiting_event(claimed.id)
            await session.commit()

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok = await svc.refresh_heartbeat(claimed.id, _WORKER_A)
            await session.commit()

        assert ok is False

        # Status must still be awaiting_event.
        async with session_factory() as session:
            row = await session.execute(
                sa.select(PipelineRun).where(PipelineRun.id == claimed.id).execution_options(populate_existing=True)
            )
            final = row.scalar_one()
        assert final.status == PipelineRunStatus.awaiting_event

    async def test_refresh_heartbeat_emits_no_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """refresh_heartbeat must not emit any domain events."""
        capturing = CapturingEventService()
        claimed = await _claim_run(session_factory, capturing)
        capturing.clear()  # reset — only care about refresh events

        async with session_factory() as session:
            svc = _make_svc(session, capturing)
            ok = await svc.refresh_heartbeat(claimed.id, _WORKER_A)
            await session.commit()

        assert ok is True
        assert capturing.emitted == [], f'Expected no events, got: {capturing.emitted}'
