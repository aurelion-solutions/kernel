# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit and integration tests for reclaim_sweep_tick and drain_active_run in runner.py.

DB-backed tests use real PostgreSQL (session_factory from root conftest).
Mock-based tests use AsyncMock to avoid DB access.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
)
from src.platform.orchestrator.runner import (
    RunHandle,
    drain_active_run,
    reclaim_sweep_tick,
)
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    pipeline_name: str = 'sweep_pipe',
    worker_id: str = 'host-sweep-1-0',
) -> PipelineRun:
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
        )
        run = result.run
        await svc.mark_pipeline_running(run.id, worker_id=worker_id)
        await session.commit()
        return run


async def _make_stale(session_factory: async_sessionmaker[AsyncSession], run_id: object) -> None:
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE pipeline_runs SET last_heartbeat_at = now() - interval '30 seconds' WHERE id = :rid"),
            {'rid': run_id},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Tests — reclaim_sweep_tick (DB-backed)
# ---------------------------------------------------------------------------


class TestReclaimSweepTick:
    async def test_empty_queue_noop(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """No stale runs → sweep_tick completes without error."""
        capturing = CapturingEventService()
        await reclaim_sweep_tick(
            session_factory,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        # No heartbeat_lost events.
        hb_events = [e for e in capturing.emitted if e.event_type == 'pipeline.run.heartbeat_lost']
        assert hb_events == []

    async def test_one_stale_run_reclaimed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One stale run → reclaimed, events emitted, available for claim afterwards."""
        capturing = CapturingEventService()
        run = await _insert_running(session_factory, capturing)
        await _make_stale(session_factory, run.id)
        capturing.clear()

        await reclaim_sweep_tick(
            session_factory,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )

        # heartbeat_lost event emitted.
        hb_events = [e for e in capturing.emitted if e.event_type == 'pipeline.run.heartbeat_lost']
        assert len(hb_events) == 1
        assert hb_events[0].payload['run_id'] == str(run.id)

        # Row is now pending — available for claim.
        async with session_factory() as session:
            row = await session.get(PipelineRun, run.id)
        assert row is not None
        assert row.status == PipelineRunStatus.pending

    async def test_row_failure_does_not_stop_others(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """If reclaim_stale_run raises for row K, rows K+1..N are still processed."""
        capturing = CapturingEventService()

        run_bad = await _insert_running(session_factory, capturing, pipeline_name='bad_pipe')
        run_good = await _insert_running(session_factory, capturing, pipeline_name='good_pipe')
        await _make_stale(session_factory, run_bad.id)
        await _make_stale(session_factory, run_good.id)
        capturing.clear()

        warning_logged = []
        mock_logs = MagicMock()

        def _emit_safe(
            *,
            level: LogLevel,
            message: str,
            component: str,
            payload: dict[str, object],
            correlation_id: str | None = None,
        ) -> None:
            if level == LogLevel.WARNING:
                warning_logged.append(message)

        mock_logs.emit_safe = _emit_safe

        call_count = 0
        original_reclaim = PipelineOrchestratorService.reclaim_stale_run

        async def _patched_reclaim(self: PipelineOrchestratorService, rid: uuid.UUID, **kwargs: object) -> bool:
            nonlocal call_count
            call_count += 1
            if rid == run_bad.id:
                raise RuntimeError('simulated reclaim failure')
            return await original_reclaim(self, rid)

        with patch.object(PipelineOrchestratorService, 'reclaim_stale_run', _patched_reclaim):
            await reclaim_sweep_tick(
                session_factory,
                events=EventService(sink=capturing),
                logs=mock_logs,
            )

        # good run should be reclaimed regardless.
        async with session_factory() as session:
            good_row = await session.get(PipelineRun, run_good.id)
        assert good_row is not None
        assert good_row.status == PipelineRunStatus.pending

        # Warning logged for bad row.
        assert any('Reclaim sweep row failed' in w for w in warning_logged)


# ---------------------------------------------------------------------------
# Tests — drain_active_run (mock-based, no DB)
# ---------------------------------------------------------------------------


class TestDrainActiveRun:
    async def test_clean_exit_no_reclaim(self) -> None:
        """completion_event set before timeout → drain returns cleanly, no reclaim."""
        capturing = CapturingEventService()
        run_id = uuid.uuid4()
        completion_event = asyncio.Event()

        # Pre-set so drain completes immediately.
        completion_event.set()

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[AsyncMock]:
            yield mock_session

        mock_factory = MagicMock(side_effect=lambda *a, **kw: _ctx())

        reclaim_called = False
        with patch('src.platform.orchestrator.runner.PipelineOrchestratorService') as MockSvc:
            instance = AsyncMock()

            async def _reclaim(*args: object, **kwargs: object) -> bool:
                nonlocal reclaim_called
                reclaim_called = True
                return True

            instance.reclaim_stale_run = _reclaim
            MockSvc.return_value = instance

            await drain_active_run(
                mock_factory,
                run_id=run_id,
                completion_event=completion_event,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
                drain_timeout=5.0,
            )

        assert not reclaim_called

    async def test_timeout_triggers_reclaim(self) -> None:
        """completion_event never set → drain times out, reclaim_stale_run called."""
        capturing = CapturingEventService()
        run_id = uuid.uuid4()
        completion_event = asyncio.Event()  # never set

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[AsyncMock]:
            yield mock_session

        mock_factory = MagicMock(side_effect=lambda *a, **kw: _ctx())

        reclaim_called = False
        with patch('src.platform.orchestrator.runner.PipelineOrchestratorService') as MockSvc:
            instance = AsyncMock()

            async def _reclaim(*args: object, **kwargs: object) -> bool:
                nonlocal reclaim_called
                reclaim_called = True
                return True

            instance.reclaim_stale_run = _reclaim
            MockSvc.return_value = instance

            await drain_active_run(
                mock_factory,
                run_id=run_id,
                completion_event=completion_event,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
                drain_timeout=0.05,  # very short timeout for test speed
            )

        assert reclaim_called


# ---------------------------------------------------------------------------
# Tests — RunHandle
# ---------------------------------------------------------------------------


class TestRunHandle:
    def test_default_initialization(self) -> None:
        """RunHandle initializes with None run_id and a fresh asyncio.Event."""
        handle = RunHandle()
        assert handle.run_id is None
        assert isinstance(handle.completion, asyncio.Event)
        assert not handle.completion.is_set()

    def test_can_set_run_id(self) -> None:
        rid = uuid.uuid4()
        handle = RunHandle()
        handle.run_id = rid
        assert handle.run_id == rid
