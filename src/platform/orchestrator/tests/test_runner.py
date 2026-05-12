# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for runner.py.

run_one_iteration tests use real Postgres (session_factory).
_resolve_templates tests are pure-Python, no DB required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from pydantic import BaseModel
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import (
    PipelineEventWaiter,
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
    StepRun,
    StepRunStatus,
)
from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionContext, register_action
from src.platform.orchestrator.runner import (
    WorkerIdentity,
    _heartbeat_refresher,
    _parse_duration,
    _resolve_templates,
    run_one_iteration,
)
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker() -> WorkerIdentity:
    return WorkerIdentity(worker_id='test-host-1234-0', hostname='test-host', pid=1234, slot_index=0)


def _make_svc(session: AsyncSession, capturing: CapturingEventService) -> PipelineOrchestratorService:
    return PipelineOrchestratorService(
        session=session,
        events=EventService(sink=capturing),
        logs=NoOpLogService(),
    )


async def _insert_pending(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    *,
    pipeline_name: str = 'test_pipe',
    pipeline_version: int = 1,
    args: dict | None = None,
) -> PipelineRun:
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            args=args or {},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='test-corr',
        )
        await session.commit()
        return result.run


def _make_defn(steps: list[dict]) -> PipelineDefinition:
    """Build a minimal PipelineDefinition for tests."""
    from pathlib import Path  # noqa: PLC0415

    return PipelineDefinition(
        name='test_pipe',
        version=1,
        schema_version=1,
        source_path=Path('/fake/test_pipe.yaml'),
        content_hash='abc123',
        args_schema_dict={},
        triggers=(),
        steps=tuple(steps),
        raw_dict={},
    )


class _FakeLoader:
    """In-memory pipeline loader for tests."""

    def __init__(self, defn: PipelineDefinition | None) -> None:
        self._defn = defn

    def get(self, name: str, version: int) -> PipelineDefinition | None:  # noqa: ARG002
        return self._defn


# ---------------------------------------------------------------------------
# Pydantic schemas for synthetic actions
# ---------------------------------------------------------------------------


class _EchoArgs(BaseModel):
    value: str


class _EchoResult(BaseModel):
    echo: str


class _FailArgs(BaseModel):
    msg: str


class _FailResult(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Tests — WorkerIdentity
# ---------------------------------------------------------------------------


class TestWorkerIdentity:
    def test_create_format(self) -> None:
        identity = WorkerIdentity.create(slot_index=3)
        # hostname-pid-slot; hostname may itself contain dashes
        assert identity.worker_id.endswith(f'-{identity.pid}-3')
        assert identity.slot_index == 3


# ---------------------------------------------------------------------------
# Tests — _resolve_templates
# ---------------------------------------------------------------------------


class TestResolveTemplates:
    def test_args_ref(self) -> None:
        result = _resolve_templates(
            '${args.name}',
            pipeline_args={'name': 'alice'},
            step_results={},
        )
        assert result == 'alice'

    def test_args_ref_non_string_native_type(self) -> None:
        result = _resolve_templates(
            '${args.count}',
            pipeline_args={'count': 42},
            step_results={},
        )
        assert result == 42
        assert isinstance(result, int)

    def test_steps_result_ref(self) -> None:
        result = _resolve_templates(
            '${steps.s1.result.key}',
            pipeline_args={},
            step_results={'s1': {'result': {'key': 'val'}}},
        )
        assert result == 'val'

    def test_steps_nested_path(self) -> None:
        result = _resolve_templates(
            '${steps.s1.result.a.b}',
            pipeline_args={},
            step_results={'s1': {'result': {'a': {'b': 99}}}},
        )
        assert result == 99

    def test_mixed_string(self) -> None:
        result = _resolve_templates(
            'hello ${args.name}, count=${args.count}',
            pipeline_args={'name': 'bob', 'count': 7},
            step_results={},
        )
        assert result == 'hello bob, count=7'

    def test_nested_dict(self) -> None:
        value = {'x': '${args.x}', 'nested': {'y': '${args.y}'}}
        result = _resolve_templates(value, pipeline_args={'x': 1, 'y': 2}, step_results={})
        assert result == {'x': 1, 'nested': {'y': 2}}

    def test_nested_list(self) -> None:
        value = ['${args.a}', 'literal', '${args.b}']
        result = _resolve_templates(value, pipeline_args={'a': 'A', 'b': 'B'}, step_results={})
        assert result == ['A', 'literal', 'B']

    def test_non_string_passthrough(self) -> None:
        assert _resolve_templates(42, pipeline_args={}, step_results={}) == 42
        assert _resolve_templates(None, pipeline_args={}, step_results={}) is None

    def test_missing_key_raises(self) -> None:
        with pytest.raises(KeyError):
            _resolve_templates('${args.missing}', pipeline_args={}, step_results={})


# ---------------------------------------------------------------------------
# Tests — run_one_iteration
# ---------------------------------------------------------------------------


class TestRunOneIteration:
    async def test_idle_when_no_pending_run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Empty table → run_one_iteration returns 'idle'."""
        capturing = CapturingEventService()
        loader = _FakeLoader(None)
        worker = _make_worker()

        outcome = await run_one_iteration(
            session_factory,
            worker=worker,
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        assert outcome == 'idle'

    async def test_completed_happy_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """One pending run + engine_call step → run completed, step completed."""
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()

        @register_action('test_engine', 'echo', _EchoArgs, _EchoResult)
        async def _echo(args: _EchoArgs, ctx: ActionContext) -> dict:
            return {'echo': args.value}

        try:
            defn = _make_defn(
                [
                    {
                        'name': 'step1',
                        'kind': 'engine_call',
                        'engine': 'test_engine',
                        'action': 'echo',
                        'args': {'value': 'hello'},
                    }
                ]
            )
            loader = _FakeLoader(defn)
            run = await _insert_pending(session_factory, capturing)
            capturing.clear()

            outcome = await run_one_iteration(
                session_factory,
                worker=_make_worker(),
                pipeline_loader=loader,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
            )
            assert outcome == 'completed'

            # Verify DB state.
            async with session_factory() as session:
                row = await session.execute(
                    sa.select(PipelineRun).where(PipelineRun.id == run.id).execution_options(populate_existing=True)
                )
                final_run = row.scalar_one()

            assert final_run.status == PipelineRunStatus.completed

            # Events emitted: started, step.started, step.completed, run.completed.
            types = [e.event_type for e in capturing.emitted]
            assert 'pipeline.run.started' in types
            assert 'pipeline.step.started' in types
            assert 'pipeline.step.completed' in types
            assert 'pipeline.run.completed' in types

        finally:
            ACTION_REGISTRY._clear_for_tests()

    async def test_action_raises_run_and_step_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Action raises RuntimeError → run failed, step failed, events emitted."""
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()

        @register_action('test_engine', 'boom', _FailArgs, _FailResult)
        async def _boom(args: _FailArgs, ctx: ActionContext) -> dict:
            raise RuntimeError(args.msg)

        try:
            defn = _make_defn(
                [
                    {
                        'name': 'step_fail',
                        'kind': 'engine_call',
                        'engine': 'test_engine',
                        'action': 'boom',
                        'args': {'msg': 'exploded'},
                    }
                ]
            )
            loader = _FakeLoader(defn)
            await _insert_pending(session_factory, capturing)
            capturing.clear()

            outcome = await run_one_iteration(
                session_factory,
                worker=_make_worker(),
                pipeline_loader=loader,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
            )
            assert outcome == 'failed'

            types = [e.event_type for e in capturing.emitted]
            assert 'pipeline.step.failed' in types
            assert 'pipeline.run.failed' in types

        finally:
            ACTION_REGISTRY._clear_for_tests()

    async def test_unsupported_step_kind_run_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Unsupported step kind (not wait_for_event, no engine/action) → run failed."""
        capturing = CapturingEventService()
        defn = _make_defn(
            [
                {
                    'name': 'x',
                    'type': 'some_future_kind',
                }
            ]
        )
        loader = _FakeLoader(defn)
        await _insert_pending(session_factory, capturing)
        capturing.clear()

        outcome = await run_one_iteration(
            session_factory,
            worker=_make_worker(),
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        assert outcome == 'failed'

        failed_events = [e for e in capturing.emitted if e.event_type == 'pipeline.run.failed']
        assert len(failed_events) == 1
        assert 'unsupported step kind: some_future_kind' in failed_events[0].payload['error']

    async def test_missing_definition_run_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """loader.get returns None → run marked failed."""
        capturing = CapturingEventService()
        loader = _FakeLoader(None)
        await _insert_pending(session_factory, capturing)
        capturing.clear()

        outcome = await run_one_iteration(
            session_factory,
            worker=_make_worker(),
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        assert outcome == 'failed'

        failed_events = [e for e in capturing.emitted if e.event_type == 'pipeline.run.failed']
        assert len(failed_events) == 1
        assert failed_events[0].payload['error'] == 'pipeline definition not found'


# ---------------------------------------------------------------------------
# Tests — _heartbeat_refresher (unit, mocked session_factory)
# ---------------------------------------------------------------------------


class TestHeartbeatRefresher:
    async def test_ticks_at_interval(self) -> None:
        """With 50ms interval, refresher should tick ~4-6 times in 250ms."""
        capturing = CapturingEventService()
        call_count = 0

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[AsyncMock]:
            yield mock_session

        def _factory(*args: object, **kwargs: object) -> object:
            return _ctx()

        mock_factory = MagicMock(side_effect=_factory)
        stop_event = asyncio.Event()
        run_id = uuid.uuid4()

        with patch('src.platform.orchestrator.runner.PipelineOrchestratorService') as MockSvc:
            instance = AsyncMock()

            async def _count_calls(rid: uuid.UUID, wid: str) -> bool:
                nonlocal call_count
                call_count += 1
                return True

            instance.refresh_heartbeat = _count_calls
            MockSvc.return_value = instance

            task = asyncio.create_task(
                _heartbeat_refresher(
                    mock_factory,
                    run_id=run_id,
                    worker_id='w-1',
                    events=EventService(sink=capturing),
                    logs=NoOpLogService(),
                    stop_event=stop_event,
                    cancel_event=asyncio.Event(),
                    interval_seconds=0.05,
                )
            )

            await asyncio.sleep(0.25)
            stop_event.set()
            await asyncio.wait_for(task, timeout=0.5)

        assert 4 <= call_count <= 7, f'Expected 4-7 ticks, got {call_count}'

    async def test_stops_on_event(self) -> None:
        """Set stop_event immediately after start → task exits within 100ms."""
        capturing = CapturingEventService()
        stop_event = asyncio.Event()
        run_id = uuid.uuid4()

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[AsyncMock]:
            yield mock_session

        mock_factory = MagicMock(side_effect=lambda *a, **kw: _ctx())

        with patch('src.platform.orchestrator.runner.PipelineOrchestratorService') as MockSvc:
            instance = AsyncMock()
            instance.refresh_heartbeat = AsyncMock(return_value=True)
            MockSvc.return_value = instance

            task = asyncio.create_task(
                _heartbeat_refresher(
                    mock_factory,
                    run_id=run_id,
                    worker_id='w-1',
                    events=EventService(sink=capturing),
                    logs=NoOpLogService(),
                    stop_event=stop_event,
                    cancel_event=asyncio.Event(),
                    interval_seconds=10.0,  # long interval — stop_event should cut it
                )
            )

            # Set event almost immediately.
            await asyncio.sleep(0.01)
            stop_event.set()
            await asyncio.wait_for(task, timeout=0.1)

        assert task.done()

    async def test_continues_on_false(self) -> None:
        """refresh_heartbeat returns False, True, True → loop survives, emits WARNING."""
        capturing = CapturingEventService()
        stop_event = asyncio.Event()
        run_id = uuid.uuid4()
        call_results = [False, True, True]
        call_count = 0

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[AsyncMock]:
            yield mock_session

        mock_factory = MagicMock(side_effect=lambda *a, **kw: _ctx())

        warning_messages: list[str] = []
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
                warning_messages.append(message)

        mock_logs.emit_safe = _emit_safe

        with patch('src.platform.orchestrator.runner.PipelineOrchestratorService') as MockSvc:
            instance = AsyncMock()

            async def _side(*args: object, **kwargs: object) -> bool:
                nonlocal call_count
                val = call_results[call_count] if call_count < len(call_results) else True
                call_count += 1
                if call_count >= len(call_results):
                    stop_event.set()
                return val

            instance.refresh_heartbeat = _side
            MockSvc.return_value = instance

            task = asyncio.create_task(
                _heartbeat_refresher(
                    mock_factory,
                    run_id=run_id,
                    worker_id='w-1',
                    events=EventService(sink=capturing),
                    logs=mock_logs,
                    stop_event=stop_event,
                    cancel_event=asyncio.Event(),
                    interval_seconds=0.01,
                )
            )
            await asyncio.wait_for(task, timeout=2.0)

        assert call_count >= 3
        assert any('Heartbeat refresh missed (rowcount=0)' in m for m in warning_messages)

    async def test_continues_on_exception(self) -> None:
        """refresh_heartbeat raises RuntimeError on first call → loop survives, emits WARNING."""
        capturing = CapturingEventService()
        stop_event = asyncio.Event()
        run_id = uuid.uuid4()
        call_count = 0

        mock_session = AsyncMock(spec=AsyncSession)
        mock_session.commit = AsyncMock()

        @asynccontextmanager
        async def _ctx() -> AsyncGenerator[AsyncMock]:
            yield mock_session

        mock_factory = MagicMock(side_effect=lambda *a, **kw: _ctx())

        warning_messages: list[str] = []
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
                warning_messages.append(message)

        mock_logs.emit_safe = _emit_safe

        with patch('src.platform.orchestrator.runner.PipelineOrchestratorService') as MockSvc:
            instance = AsyncMock()

            async def _side(*args: object, **kwargs: object) -> bool:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError('db blip')
                stop_event.set()
                return True

            instance.refresh_heartbeat = _side
            MockSvc.return_value = instance

            task = asyncio.create_task(
                _heartbeat_refresher(
                    mock_factory,
                    run_id=run_id,
                    worker_id='w-1',
                    events=EventService(sink=capturing),
                    logs=mock_logs,
                    stop_event=stop_event,
                    cancel_event=asyncio.Event(),
                    interval_seconds=0.01,
                )
            )
            await asyncio.wait_for(task, timeout=2.0)

        assert call_count >= 2
        assert any('Heartbeat refresh raised' in m for m in warning_messages)


# ---------------------------------------------------------------------------
# Tests — _parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    @pytest.mark.parametrize(
        ('raw', 'expected_seconds'),
        [
            ('1s', 1),
            ('30s', 30),
            ('1m', 60),
            ('30m', 1800),
            ('2h', 7200),
            ('7d', 604800),
        ],
    )
    def test_valid(self, raw: str, expected_seconds: int) -> None:
        result = _parse_duration(raw)
        assert result == timedelta(seconds=expected_seconds)

    @pytest.mark.parametrize(
        'raw',
        ['0s', '1x', '', '7', '-5m', '1.5m', '0m', '0h', '0d'],
    )
    def test_invalid_raises(self, raw: str) -> None:
        with pytest.raises(ValueError):
            _parse_duration(raw)


# ---------------------------------------------------------------------------
# Tests — wait_for_event step
# ---------------------------------------------------------------------------


def _make_wait_for_event_step(
    *,
    event: str = 'employee.hired',
    match: dict[str, Any] | None = None,
    timeout: str = '30m',
    on_timeout: str = 'fail',
    name: str = 'wait_step',
) -> dict[str, Any]:
    return {
        'name': name,
        'type': 'wait_for_event',
        'event': event,
        'match': match or {'department': 'engineering'},
        'timeout': timeout,
        'on_timeout': on_timeout,
    }


class TestWaitForEventStep:
    async def test_wait_for_event_step_parks(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """wait_for_event step → PipelineRun awaiting_event, StepRun awaiting_event,
        PipelineEventWaiter created."""
        capturing = CapturingEventService()
        defn = _make_defn([_make_wait_for_event_step()])
        loader = _FakeLoader(defn)
        run = await _insert_pending(session_factory, capturing)
        capturing.clear()

        before = datetime.now(UTC)
        outcome = await run_one_iteration(
            session_factory,
            worker=_make_worker(),
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        after = datetime.now(UTC)

        assert outcome == 'awaiting_event'

        async with session_factory() as session:
            final_run = (
                await session.execute(
                    sa.select(PipelineRun).where(PipelineRun.id == run.id).execution_options(populate_existing=True)
                )
            ).scalar_one()
            step_run = (await session.execute(sa.select(StepRun).where(StepRun.pipeline_run_id == run.id))).scalar_one()
            waiter = (
                await session.execute(
                    sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_run.id)
                )
            ).scalar_one()

        # PipelineRun
        assert final_run.status == PipelineRunStatus.awaiting_event
        assert final_run.worker_id is None
        assert final_run.last_heartbeat_at is None

        # StepRun
        assert step_run.status == StepRunStatus.awaiting_event
        assert step_run.args['event'] == 'employee.hired'
        assert step_run.args['match'] == {'department': 'engineering'}
        assert step_run.args['timeout'] == '30m'
        assert step_run.args['on_timeout'] == 'fail'

        # PipelineEventWaiter
        assert waiter.event_type == 'employee.hired'
        assert waiter.match == {'department': 'engineering'}
        expected_delta = timedelta(minutes=30)
        assert before + expected_delta <= waiter.expires_at <= after + expected_delta + timedelta(seconds=2)

    async def test_wait_for_event_returns_awaiting_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """run_one_iteration returns the literal 'awaiting_event'."""
        capturing = CapturingEventService()
        defn = _make_defn([_make_wait_for_event_step()])
        loader = _FakeLoader(defn)
        await _insert_pending(session_factory, capturing)
        capturing.clear()

        outcome = await run_one_iteration(
            session_factory,
            worker=_make_worker(),
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        assert outcome == 'awaiting_event'

    async def test_wait_for_event_invalid_timeout_pipeline_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Invalid timeout → mark_pipeline_failed, no StepRun and no waiter created."""
        capturing = CapturingEventService()
        defn = _make_defn([_make_wait_for_event_step(timeout='1x')])
        loader = _FakeLoader(defn)
        run = await _insert_pending(session_factory, capturing)
        capturing.clear()

        outcome = await run_one_iteration(
            session_factory,
            worker=_make_worker(),
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        assert outcome == 'failed'

        async with session_factory() as session:
            step_runs = (
                (await session.execute(sa.select(StepRun).where(StepRun.pipeline_run_id == run.id))).scalars().all()
            )
            waiters = (
                (
                    await session.execute(
                        sa.select(PipelineEventWaiter)
                        .join(StepRun, PipelineEventWaiter.step_run_id == StepRun.id)
                        .where(StepRun.pipeline_run_id == run.id)
                    )
                )
                .scalars()
                .all()
            )

        assert step_runs == [], 'No StepRun should be created on timeout parse failure'
        assert waiters == [], 'No PipelineEventWaiter should be created on timeout parse failure'

        failed_events = [e for e in capturing.emitted if e.event_type == 'pipeline.run.failed']
        assert len(failed_events) == 1
        assert 'invalid timeout' in failed_events[0].payload['error']

    async def test_wait_for_event_template_resolution_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Template resolution failure in match → mark_pipeline_failed, no StepRun/waiter."""
        capturing = CapturingEventService()
        defn = _make_defn(
            [
                _make_wait_for_event_step(
                    match={'dep': '${args.missing_key}'},
                )
            ]
        )
        loader = _FakeLoader(defn)
        run = await _insert_pending(session_factory, capturing, args={})
        capturing.clear()

        outcome = await run_one_iteration(
            session_factory,
            worker=_make_worker(),
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        assert outcome == 'failed'

        async with session_factory() as session:
            step_runs = (
                (await session.execute(sa.select(StepRun).where(StepRun.pipeline_run_id == run.id))).scalars().all()
            )
            waiters = (
                (
                    await session.execute(
                        sa.select(PipelineEventWaiter)
                        .join(StepRun, PipelineEventWaiter.step_run_id == StepRun.id)
                        .where(StepRun.pipeline_run_id == run.id)
                    )
                )
                .scalars()
                .all()
            )

        assert step_runs == [], 'No StepRun should be created on template resolution failure'
        assert waiters == [], 'No PipelineEventWaiter should be created on template resolution failure'

        failed_events = [e for e in capturing.emitted if e.event_type == 'pipeline.run.failed']
        assert len(failed_events) == 1
        assert 'template resolution failed' in failed_events[0].payload['error']

    async def test_wait_for_event_slot_freed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """After wait_for_event park, a second pending run can be claimed by the same worker."""
        capturing = CapturingEventService()
        defn = _make_defn([_make_wait_for_event_step()])
        loader = _FakeLoader(defn)

        # Create two pending runs with distinct args to avoid content_hash dedupe.
        run1 = await _insert_pending(session_factory, capturing, pipeline_name='test_pipe', args={'slot': 1})
        run2 = await _insert_pending(session_factory, capturing, pipeline_name='test_pipe', args={'slot': 2})
        capturing.clear()

        worker = _make_worker()

        # First iteration — parks run1.
        outcome1 = await run_one_iteration(
            session_factory,
            worker=worker,
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        assert outcome1 == 'awaiting_event'

        # Verify run1 is parked (worker_id cleared).
        async with session_factory() as session:
            parked = (
                await session.execute(
                    sa.select(PipelineRun).where(PipelineRun.id == run1.id).execution_options(populate_existing=True)
                )
            ).scalar_one()
        assert parked.worker_id is None

        # Second iteration — same worker should claim run2.
        outcome2 = await run_one_iteration(
            session_factory,
            worker=worker,
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        # run2 will also park (same definition).
        assert outcome2 == 'awaiting_event'

        async with session_factory() as session:
            run2_row = (
                await session.execute(
                    sa.select(PipelineRun).where(PipelineRun.id == run2.id).execution_options(populate_existing=True)
                )
            ).scalar_one()
        assert run2_row.status == PipelineRunStatus.awaiting_event
