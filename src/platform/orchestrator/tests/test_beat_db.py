# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-backed integration tests for beat.py.

Uses real PostgreSQL via session_factory from root conftest.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.beat import (
    _BEAT_LOCK_KEY,
    already_fired_in_window,
    beat_tick,
    fire_schedule,
)
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import PipelineRun, PipelineTriggerSource
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def capturing() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing: CapturingEventService) -> EventService:
    return EventService(sink=capturing)


def _make_service(session: AsyncSession, event_service: EventService) -> PipelineOrchestratorService:
    return PipelineOrchestratorService(session=session, events=event_service, logs=NoOpLogService())


def _make_defn(
    name: str = 'test_pipe',
    version: int = 1,
    *,
    triggers: list[dict[str, Any]] | None = None,
) -> PipelineDefinition:
    """Build a minimal PipelineDefinition for tests."""
    trigs = triggers if triggers is not None else [{'type': 'schedule', 'every': '5m'}]
    return PipelineDefinition(
        name=name,
        version=version,
        schema_version=1,
        source_path=Path('/fake/pipeline.yaml'),
        content_hash='aabbcc',
        args_schema_dict={},
        triggers=tuple(trigs),
        steps=(),
        raw_dict={},
    )


def _service_factory(
    session_factory: async_sessionmaker[AsyncSession],
    event_service: EventService,
) -> Callable[[AsyncSession], PipelineOrchestratorService]:
    def factory(session: AsyncSession) -> PipelineOrchestratorService:
        return _make_service(session, event_service)

    return factory


# ---------------------------------------------------------------------------
# already_fired_in_window
# ---------------------------------------------------------------------------


class TestAlreadyFiredInWindow:
    async def test_empty_table_returns_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        fp = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
        async with session_factory() as session:
            result = await already_fired_in_window(session, 'pipe_a', 1, fp)
        assert result is False

    async def test_schedule_run_in_window_returns_true(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
    ) -> None:
        fp = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
        # Insert a schedule run with created_at >= fp
        async with session_factory() as session:
            svc = _make_service(session, event_service)
            await svc.create_pipeline_run('pipe_b', 1, {}, PipelineTriggerSource.schedule)
            await session.commit()

        async with session_factory() as session:
            result = await already_fired_in_window(session, 'pipe_b', 1, fp)
        assert result is True

    async def test_http_run_in_window_does_not_block_schedule(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
    ) -> None:
        fp = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
        async with session_factory() as session:
            svc = _make_service(session, event_service)
            await svc.create_pipeline_run('pipe_c', 1, {}, PipelineTriggerSource.http)
            await session.commit()

        async with session_factory() as session:
            result = await already_fired_in_window(session, 'pipe_c', 1, fp)
        # http run must NOT count as schedule-fired
        assert result is False


# ---------------------------------------------------------------------------
# fire_schedule
# ---------------------------------------------------------------------------


class TestFireSchedule:
    async def test_happy_path_inserts_run_with_scheduled_at(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
    ) -> None:
        now = datetime(2026, 5, 10, 12, 7, 0, tzinfo=UTC)
        defn = _make_defn(name='fire_pipe', version=1)
        trigger = {'type': 'schedule', 'every': '5m'}

        async with session_factory() as session:
            svc = _make_service(session, event_service)
            run_id = await fire_schedule(svc, defn, trigger, now=now)
            await session.commit()

        assert run_id is not None

        # Verify the row exists and has _scheduled_at in args.
        async with session_factory() as session:
            row = await session.get(PipelineRun, run_id)
        assert row is not None
        assert row.trigger_source == PipelineTriggerSource.schedule
        assert '_scheduled_at' in (row.args or {})

    async def test_dedupe_second_call_returns_none(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
    ) -> None:
        now = datetime(2026, 5, 10, 12, 7, 0, tzinfo=UTC)
        defn = _make_defn(name='dedupe_pipe', version=1)
        trigger: dict[str, Any] = {'type': 'schedule', 'every': '5m'}

        async with session_factory() as session:
            svc = _make_service(session, event_service)
            first = await fire_schedule(svc, defn, trigger, now=now)
            await session.commit()

        async with session_factory() as session:
            svc = _make_service(session, event_service)
            second = await fire_schedule(svc, defn, trigger, now=now)
            await session.commit()

        assert first is not None
        assert second is None

        # Only 1 row in table for this pipeline.
        async with session_factory() as session:
            count_result = await session.execute(
                sa.select(sa.func.count()).where(PipelineRun.pipeline_name == 'dedupe_pipe')
            )
        assert count_result.scalar() == 1


# ---------------------------------------------------------------------------
# beat_tick
# ---------------------------------------------------------------------------


class TestBeatTick:
    async def test_happy_path_inserts_one_row(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
    ) -> None:
        now = datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC)
        defn = _make_defn(name='tick_pipe', triggers=[{'type': 'schedule', 'every': '5m'}])
        defs: Mapping[str, PipelineDefinition] = {'tick_pipe': defn}

        result = await beat_tick(
            session_factory,
            defs,
            _service_factory(session_factory, event_service),
            NoOpLogService(),
            now=now,
        )

        assert result.lock_acquired is True
        assert len(result.fired_run_ids) == 1
        assert result.skipped_count == 0

    async def test_skipped_when_prior_schedule_run_exists(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
    ) -> None:
        now = datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC)
        defn = _make_defn(name='skip_pipe', triggers=[{'type': 'schedule', 'every': '5m'}])
        defs: Mapping[str, PipelineDefinition] = {'skip_pipe': defn}
        factory = _service_factory(session_factory, event_service)

        # First tick — fires.
        r1 = await beat_tick(session_factory, defs, factory, NoOpLogService(), now=now)
        assert len(r1.fired_run_ids) == 1

        # Second tick in same window — skips.
        r2 = await beat_tick(session_factory, defs, factory, NoOpLogService(), now=now)
        assert len(r2.fired_run_ids) == 0
        assert r2.skipped_count == 1

    async def test_lock_contention_returns_lock_acquired_false(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
    ) -> None:
        """Pre-acquire the advisory lock on a separate connection; beat_tick must back off."""
        from src.conftest import TEST_DATABASE_URL  # noqa: PLC0415

        side_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        side_conn = await side_engine.connect()
        try:
            # Acquire the lock on the side connection (pg_try_advisory_lock is session-level).
            await side_conn.execute(sa.select(sa.func.pg_advisory_lock(_BEAT_LOCK_KEY)))

            now = datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC)
            defn = _make_defn(name='contend_pipe', triggers=[{'type': 'schedule', 'every': '5m'}])
            defs: Mapping[str, PipelineDefinition] = {'contend_pipe': defn}

            result = await beat_tick(
                session_factory,
                defs,
                _service_factory(session_factory, event_service),
                NoOpLogService(),
                now=now,
            )
            assert result.lock_acquired is False
            assert len(result.fired_run_ids) == 0

            # Release lock and verify next tick succeeds.
            await side_conn.execute(sa.select(sa.func.pg_advisory_unlock(_BEAT_LOCK_KEY)))

            result2 = await beat_tick(
                session_factory,
                defs,
                _service_factory(session_factory, event_service),
                NoOpLogService(),
                now=now,
            )
            assert result2.lock_acquired is True
            assert len(result2.fired_run_ids) == 1
        finally:
            await side_conn.close()
            await side_engine.dispose()

    async def test_resilience_one_bad_pipeline_does_not_block_other(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pipeline A raises; pipeline B must still be fired."""
        now = datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC)
        defn_a = _make_defn(name='bad_pipe', triggers=[{'type': 'schedule', 'every': '5m'}])
        defn_b = _make_defn(name='good_pipe', version=2, triggers=[{'type': 'schedule', 'every': '5m'}])
        defs: Mapping[str, PipelineDefinition] = {'bad_pipe': defn_a, 'good_pipe': defn_b}

        original_factory = _service_factory(session_factory, event_service)

        call_count = 0

        def _patched_factory(session: AsyncSession) -> PipelineOrchestratorService:
            nonlocal call_count
            svc: PipelineOrchestratorService = original_factory(session)
            call_count += 1
            # First call (for bad_pipe) → raise.
            if call_count == 1:

                async def _raise(*args: Any, **kwargs: Any) -> Any:
                    raise RuntimeError('boom')

                monkeypatch.setattr(svc, 'create_pipeline_run', _raise)
            return svc

        result = await beat_tick(
            session_factory,
            defs,
            _patched_factory,
            NoOpLogService(),
            now=now,
        )
        # good_pipe should have fired.
        assert len(result.fired_run_ids) >= 1

    async def test_beat_tick_uses_service_not_direct_insert(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        event_service: EventService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Assert create_pipeline_run is called with schedule trigger and _scheduled_at."""
        now = datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC)
        defn = _make_defn(name='spy_pipe', triggers=[{'type': 'schedule', 'every': '5m'}])
        defs: Mapping[str, PipelineDefinition] = {'spy_pipe': defn}

        calls: list[dict[str, Any]] = []
        original_factory = _service_factory(session_factory, event_service)

        def _spy_factory(session: AsyncSession) -> PipelineOrchestratorService:
            svc: PipelineOrchestratorService = original_factory(session)
            original_create = svc.create_pipeline_run

            async def _spy(*args: Any, **kwargs: Any) -> Any:
                calls.append({'args': args, 'kwargs': kwargs})
                return await original_create(*args, **kwargs)

            monkeypatch.setattr(svc, 'create_pipeline_run', _spy)
            return svc

        await beat_tick(session_factory, defs, _spy_factory, NoOpLogService(), now=now)

        assert len(calls) == 1
        _, kwargs = calls[0]['args'], calls[0]['kwargs']
        assert kwargs.get('trigger_source') == PipelineTriggerSource.schedule
        # _scheduled_at must be in the args positional arg (index 2).
        pipeline_args = calls[0]['args'][2]
        assert '_scheduled_at' in pipeline_args
