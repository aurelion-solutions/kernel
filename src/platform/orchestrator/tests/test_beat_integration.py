# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end integration tests for beat schedule firing.

Uses real PostgreSQL.  PipelineDefinition instances are built directly
(bypassing the loader's action-ref check) to avoid needing a registered
engine action — beat only calls create_pipeline_run, not step execution.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.beat import beat_tick
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import PipelineRun, PipelineTriggerSource
from src.platform.orchestrator.service import PipelineOrchestratorService


def _capturing() -> CapturingEventService:
    return CapturingEventService()


def _event_service(cap: CapturingEventService) -> EventService:
    return EventService(sink=cap)


def _make_defn(
    name: str,
    version: int = 1,
    *,
    every: str = '5m',
) -> PipelineDefinition:
    return PipelineDefinition(
        name=name,
        version=version,
        schema_version=1,
        source_path=Path('/integration/fake.yaml'),
        content_hash='integ_hash',
        args_schema_dict={},
        triggers=({'type': 'schedule', 'every': every},),
        steps=(),
        raw_dict={},
    )


def _service_factory(
    session_factory: async_sessionmaker[AsyncSession],
    event_service: EventService,
) -> Callable[[AsyncSession], PipelineOrchestratorService]:
    def factory(session: AsyncSession) -> PipelineOrchestratorService:
        return PipelineOrchestratorService(session=session, events=event_service, logs=NoOpLogService())

    return factory


class TestBeatIntegration:
    async def test_two_ticks_in_same_window_produce_one_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Drop a 5m every schedule and run beat_tick twice in the same window.

        Exactly 1 pipeline_runs row must exist after both ticks.
        """
        cap = _capturing()
        ev = _event_service(cap)
        factory = _service_factory(session_factory, ev)

        defn = _make_defn('integ_smoke', every='5m')
        defs = {'integ_smoke': defn}

        # Both ticks use the same 'now' so they are in the same 5-minute window.
        now = datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC)

        r1 = await beat_tick(session_factory, defs, factory, NoOpLogService(), now=now)
        r2 = await beat_tick(session_factory, defs, factory, NoOpLogService(), now=now)

        assert r1.lock_acquired is True
        assert len(r1.fired_run_ids) == 1
        assert r2.lock_acquired is True
        assert r2.skipped_count == 1
        assert len(r2.fired_run_ids) == 0

        async with session_factory() as session:
            count_result = await session.execute(
                sa.select(sa.func.count()).where(
                    PipelineRun.pipeline_name == 'integ_smoke',
                    PipelineRun.trigger_source == PipelineTriggerSource.schedule,
                )
            )
        assert count_result.scalar() == 1

    async def test_restart_safety_prior_schedule_run_blocks_tick(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Insert a schedule run directly (simulates a 'previous process' run).

        A subsequent beat_tick must detect it and skip firing.
        """
        cap = _capturing()
        ev = _event_service(cap)
        factory = _service_factory(session_factory, ev)

        defn = _make_defn('restart_safety', every='5m')
        defs = {'restart_safety': defn}
        now = datetime(2026, 5, 10, 13, 0, 0, tzinfo=UTC)

        # Insert the 'previous process' schedule run before the tick.
        async with session_factory() as session:
            svc = PipelineOrchestratorService(session=session, events=ev, logs=NoOpLogService())
            await svc.create_pipeline_run(
                'restart_safety',
                1,
                {'_scheduled_at': now.isoformat()},
                PipelineTriggerSource.schedule,
            )
            await session.commit()

        # Now run beat_tick — must find the existing row and skip.
        result = await beat_tick(session_factory, defs, factory, NoOpLogService(), now=now)

        assert result.fired_run_ids == []
        assert result.skipped_count == 1

        # Still only 1 row.
        async with session_factory() as session:
            count_result = await session.execute(
                sa.select(sa.func.count()).where(
                    PipelineRun.pipeline_name == 'restart_safety',
                    PipelineRun.trigger_source == PipelineTriggerSource.schedule,
                )
            )
        assert count_result.scalar() == 1
