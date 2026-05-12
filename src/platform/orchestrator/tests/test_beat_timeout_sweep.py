# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for beat.py — timeout sweep (Phase 18 Step 16).

All tests use real PostgreSQL via session_factory from root conftest.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
    beat_tick,
)
from src.platform.orchestrator.loader import PipelineDefinition
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


def _service_factory(
    capturing: CapturingEventService,
) -> Callable[[AsyncSession], PipelineOrchestratorService]:
    def factory(session: AsyncSession) -> PipelineOrchestratorService:
        return _make_svc(session, capturing)

    return factory


def _make_defn(name: str = 'sweep_pipe') -> PipelineDefinition:
    return PipelineDefinition(
        name=name,
        version=1,
        schema_version=1,
        source_path=Path('/fake/pipeline.yaml'),
        content_hash='aabbcc',
        args_schema_dict={},
        triggers=(),
        steps=(),
        raw_dict={},
    )


async def _insert_awaiting(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    *,
    pipeline_name: str,
    expires_at: datetime,
) -> tuple[PipelineRun, StepRun, PipelineEventWaiter]:
    """Insert a run+step+waiter in awaiting_event state."""
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='sweep-test',
        )
        run = result.run
        await svc.mark_pipeline_running(run.id, worker_id='worker-1')
        step = await svc.create_step_run(run.id, 'wait_step', {})
        await svc.mark_step_awaiting_event(step.id)
        await svc.mark_pipeline_awaiting_event(run.id)
        waiter = await svc.create_pipeline_event_waiter(
            step_run_id=step.id,
            event_type='user.approved',
            match={},
            expires_at=expires_at,
        )
        await session.commit()
        return run, step, waiter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSweepTimeouts:
    async def test_tick_sweeps_expired_and_skips_fresh(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """3 expired + 1 fresh → expired_run_ids length 3; fresh waiter intact."""
        capturing = CapturingEventService()

        run_ids = []
        for i in range(3):
            run, step, _ = await _insert_awaiting(
                session_factory,
                capturing,
                pipeline_name=f'sw_exp_{i}',
                expires_at=_PAST,
            )
            run_ids.append(run.id)

        run_fresh, step_fresh, _ = await _insert_awaiting(
            session_factory,
            capturing,
            pipeline_name='sw_fresh',
            expires_at=_FUTURE,
        )
        capturing.clear()

        defs: dict[str, PipelineDefinition] = {}
        result = await beat_tick(
            session_factory,
            defs,
            _service_factory(capturing),
            NoOpLogService(),
            now=_NOW,
        )

        assert len(result.expired_run_ids) == 3
        assert result.expire_failure_count == 0
        for rid in run_ids:
            assert rid in result.expired_run_ids

        # Fresh waiter must still exist.
        async with session_factory() as session:
            fresh_waiter = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_fresh.id)
            )
            assert fresh_waiter.scalar_one_or_none() is not None

        # Both event types must be emitted for each expired run.
        step_failed = [e for e in capturing.emitted if e.event_type == 'pipeline.step.failed']
        run_failed = [e for e in capturing.emitted if e.event_type == 'pipeline.run.failed']
        assert len(step_failed) == 3
        assert len(run_failed) == 3

    async def test_per_waiter_failure_isolated(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Poisoned waiter raises; other 2 still expire; expire_failure_count == 1."""
        capturing = CapturingEventService()

        runs = []
        steps = []
        for i in range(3):
            run, step, _ = await _insert_awaiting(
                session_factory,
                capturing,
                pipeline_name=f'sw_iso_{i}',
                expires_at=_PAST,
            )
            runs.append(run)
            steps.append(step)

        # Poison the middle step: force step_run_id to not exist in step_runs
        # by raw-deleting the step row (waiter FK is CASCADE, so waiter also goes).
        # Instead, corrupt via setting step status to completed so expire returns (False, None)
        # but let's create a deeper poison: monkeypatching is better here.

        capturing.clear()

        # Wrap service_factory to raise on the middle step_run_id.
        middle_step_id = steps[1].id
        original_factory = _service_factory(capturing)

        def _poisoned_factory(session: AsyncSession) -> PipelineOrchestratorService:
            svc = original_factory(session)
            original_expire = svc.expire_event_waiter

            async def _poison_expire(
                step_run_id: Any,
                *,
                correlation_id: Any = None,
            ) -> Any:
                if step_run_id == middle_step_id:
                    raise RuntimeError('simulated DB error')
                return await original_expire(step_run_id, correlation_id=correlation_id)

            svc.expire_event_waiter = _poison_expire  # type: ignore[method-assign]
            return svc

        defs: dict[str, PipelineDefinition] = {}
        result = await beat_tick(
            session_factory,
            defs,
            _poisoned_factory,
            NoOpLogService(),
            now=_NOW,
        )

        assert result.expire_failure_count == 1
        assert len(result.expired_run_ids) == 2

    async def test_tick_skips_sweep_when_lock_not_acquired(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Pre-acquired advisory lock → beat_tick skips sweep; expired_run_ids == []."""
        from src.conftest import TEST_DATABASE_URL  # noqa: PLC0415

        capturing = CapturingEventService()
        await _insert_awaiting(
            session_factory,
            capturing,
            pipeline_name='sw_locked',
            expires_at=_PAST,
        )
        capturing.clear()

        side_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        side_conn = await side_engine.connect()
        try:
            await side_conn.execute(sa.select(sa.func.pg_advisory_lock(_BEAT_LOCK_KEY)))

            result = await beat_tick(
                session_factory,
                {},
                _service_factory(capturing),
                NoOpLogService(),
                now=_NOW,
            )
            assert result.lock_acquired is False
            assert result.expired_run_ids == []

            await side_conn.execute(sa.select(sa.func.pg_advisory_unlock(_BEAT_LOCK_KEY)))
        finally:
            await side_conn.close()
            await side_engine.dispose()

    async def test_bounded_batch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With batch=2 and 3 expired: first tick → 2 expired, second tick → 1 expired."""
        capturing = CapturingEventService()

        for i in range(3):
            await _insert_awaiting(
                session_factory,
                capturing,
                pipeline_name=f'sw_batch_{i}',
                expires_at=_PAST - timedelta(minutes=i),  # distinct expires_at for ordering
            )

        capturing.clear()

        # Patch list_expired_waiter_step_ids to enforce limit=2 regardless of the
        # module-level constant (which is captured as a default arg at define time).
        original_factory = _service_factory(capturing)

        def _bounded_factory(session: AsyncSession) -> PipelineOrchestratorService:
            svc = original_factory(session)
            original_list = svc.list_expired_waiter_step_ids

            async def _limited(now: Any, *, limit: int = 100) -> Any:
                return await original_list(now, limit=2)

            monkeypatch.setattr(svc, 'list_expired_waiter_step_ids', _limited)
            return svc

        defs: dict[str, PipelineDefinition] = {}

        result1 = await beat_tick(
            session_factory,
            defs,
            _bounded_factory,
            NoOpLogService(),
            now=_NOW,
        )
        assert len(result1.expired_run_ids) == 2

        result2 = await beat_tick(
            session_factory,
            defs,
            _bounded_factory,
            NoOpLogService(),
            now=_NOW,
        )
        assert len(result2.expired_run_ids) == 1

    async def test_integration_park_then_timeout(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Full lifecycle: park a run, fast-forward time, one tick → failed_timeout."""
        capturing = CapturingEventService()

        run, step, _ = await _insert_awaiting(
            session_factory,
            capturing,
            pipeline_name='sw_park_timeout',
            expires_at=_PAST,
        )
        capturing.clear()

        defs: dict[str, PipelineDefinition] = {}
        result = await beat_tick(
            session_factory,
            defs,
            _service_factory(capturing),
            NoOpLogService(),
            now=_NOW,
        )

        assert run.id in result.expired_run_ids

        # Verify terminal states.
        async with session_factory() as session:
            run_row = await session.get(PipelineRun, run.id)
            step_row = await session.get(StepRun, step.id)

        assert run_row is not None
        assert run_row.status == PipelineRunStatus.failed_timeout
        assert step_row is not None
        assert step_row.status == StepRunStatus.failed_timeout

        # Waiter gone.
        async with session_factory() as session:
            waiter_row = await session.execute(
                sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step.id)
            )
            assert waiter_row.scalar_one_or_none() is None

        # Both events emitted.
        event_types = [e.event_type for e in capturing.emitted]
        assert 'pipeline.step.failed' in event_types
        assert 'pipeline.run.failed' in event_types
