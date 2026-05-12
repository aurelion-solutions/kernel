# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit + DB integration tests for platform/orchestrator/matcher.py.

Unit tests cover pure helpers (_payload_satisfies_match, find_matching_mq_triggers).
DB integration tests cover matcher_tick (waiter resolve + MQ trigger fire) using
real PostgreSQL via session_factory from root conftest.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.matcher import (
    _payload_satisfies_match,
    find_matching_mq_triggers,
    matcher_tick,
)
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

_FUTURE = datetime.now(UTC) + timedelta(minutes=30)


def _make_defn(
    name: str,
    *,
    mq_routing_key: str = 'test.event',
    mq_match: dict | None = None,
    args: dict | None = None,
    args_from_payload: dict | None = None,
) -> PipelineDefinition:
    trigger: dict[str, Any] = {'type': 'mq', 'routing_key': mq_routing_key}
    if mq_match is not None:
        trigger['match'] = mq_match
    else:
        trigger['match'] = {}
    if args:
        trigger['args'] = args
    if args_from_payload:
        trigger['args_from_payload'] = args_from_payload
    return PipelineDefinition(
        name=name,
        version=1,
        schema_version=1,
        source_path=Path('/fake.yaml'),
        content_hash='deadbeef',
        args_schema_dict={},
        triggers=(trigger,),
        steps=(),
        raw_dict={},
    )


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


async def _insert_awaiting_run(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    *,
    pipeline_name: str = 'match_pipe',
    event_type: str = 'approval.granted',
    match: dict | None = None,
) -> tuple[PipelineRun, StepRun, PipelineEventWaiter]:
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=1,
            args={},
            trigger_source=PipelineTriggerSource.http,
        )
        run = result.run
        await svc.mark_pipeline_running(run.id, worker_id='w1')
        step = await svc.create_step_run(run.id, 'wait_step', {})
        await svc.mark_step_awaiting_event(step.id)
        await svc.mark_pipeline_awaiting_event(run.id)
        waiter = await svc.create_pipeline_event_waiter(
            step_run_id=step.id,
            event_type=event_type,
            match=match or {},
            expires_at=_FUTURE,
        )
        await session.commit()
        return run, step, waiter


# ---------------------------------------------------------------------------
# Unit: _payload_satisfies_match
# ---------------------------------------------------------------------------


class TestPayloadSatisfiesMatch:
    def test_empty_match_always_true(self) -> None:
        assert _payload_satisfies_match({}, {}) is True
        assert _payload_satisfies_match({}, {'foo': 'bar'}) is True

    def test_flat_key_match(self) -> None:
        assert _payload_satisfies_match({'key': 'val'}, {'key': 'val', 'extra': 1}) is True

    def test_flat_key_value_mismatch(self) -> None:
        assert _payload_satisfies_match({'key': 'val'}, {'key': 'OTHER'}) is False

    def test_nested_key_path_match(self) -> None:
        assert _payload_satisfies_match({'a': {'b': 1}}, {'a': {'b': 1, 'c': 2}}) is True

    def test_nested_key_path_mismatch(self) -> None:
        assert _payload_satisfies_match({'a': {'b': 1}}, {'a': {'b': 99}}) is False

    def test_missing_key(self) -> None:
        assert _payload_satisfies_match({'missing': 'x'}, {'other': 'x'}) is False

    def test_type_mismatch_string_vs_int(self) -> None:
        assert _payload_satisfies_match({'key': '1'}, {'key': 1}) is False

    def test_list_subset_match(self) -> None:
        assert _payload_satisfies_match({'tags': ['a']}, {'tags': ['a', 'b']}) is True

    def test_list_subset_mismatch(self) -> None:
        assert _payload_satisfies_match({'tags': ['c']}, {'tags': ['a', 'b']}) is False

    def test_nested_match_type_mismatch(self) -> None:
        """match expects dict, payload has scalar → False."""
        assert _payload_satisfies_match({'a': {'b': 1}}, {'a': 'not_a_dict'}) is False


# ---------------------------------------------------------------------------
# Unit: find_matching_mq_triggers
# ---------------------------------------------------------------------------


class TestFindMatchingMqTriggers:
    def test_no_mq_triggers_ignored(self) -> None:
        defn = PipelineDefinition(
            name='sched_only',
            version=1,
            schema_version=1,
            source_path=Path('/fake.yaml'),
            content_hash='abc',
            args_schema_dict={},
            triggers=({'type': 'schedule', 'every': '5m'},),
            steps=(),
            raw_dict={},
        )
        result = find_matching_mq_triggers({'sched_only': defn}, 'employee.created', {})
        assert result == []

    def test_matching_routing_key_empty_match(self) -> None:
        defn = _make_defn('p1', mq_routing_key='employee.created', mq_match={})
        result = find_matching_mq_triggers({'p1': defn}, 'employee.created', {'foo': 'bar'})
        assert len(result) == 1
        assert result[0][0].name == 'p1'

    def test_routing_key_mismatch_skipped(self) -> None:
        defn = _make_defn('p2', mq_routing_key='employee.created', mq_match={})
        result = find_matching_mq_triggers({'p2': defn}, 'other.event', {'foo': 'bar'})
        assert result == []

    def test_match_payload_mismatch_skipped(self) -> None:
        defn = _make_defn('p3', mq_routing_key='emp.created', mq_match={'status': 'active'})
        result = find_matching_mq_triggers({'p3': defn}, 'emp.created', {'status': 'inactive'})
        assert result == []

    def test_multiple_defs_one_matches(self) -> None:
        defn_a = _make_defn('a', mq_routing_key='ev.x', mq_match={'type': 'A'})
        defn_b = _make_defn('b', mq_routing_key='ev.x', mq_match={'type': 'B'})
        result = find_matching_mq_triggers({'a': defn_a, 'b': defn_b}, 'ev.x', {'type': 'A'})
        assert len(result) == 1
        assert result[0][0].name == 'a'


# ---------------------------------------------------------------------------
# DB Integration: matcher_tick — waiter resolution
# ---------------------------------------------------------------------------


class TestMatcherTickWaiterResolution:
    async def test_containment_match_resolves_waiter(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Matching event resolves waiter: step completed, run → pending."""
        capturing = CapturingEventService()
        run, step, _ = await _insert_awaiting_run(
            session_factory,
            capturing,
            event_type='approval.granted',
            match={'request_id': 'r1'},
        )
        capturing.clear()

        await matcher_tick(
            event_type='approval.granted',
            routing_key='approval.granted',
            payload={'request_id': 'r1', 'approver': 'alice'},
            correlation_id='cid-1',
            causation_id=None,
            session_factory=session_factory,
            defs_provider=lambda: {},
            service_factory=_service_factory(capturing),
            log_service=NoOpLogService(),
        )

        # Waiter gone.
        async with session_factory() as session:
            w = await session.execute(sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step.id))
            assert w.scalar_one_or_none() is None

        # Step completed.
        async with session_factory() as session:
            s = await session.get(StepRun, step.id)
        assert s is not None and s.status == StepRunStatus.completed

        # Run → pending.
        async with session_factory() as session:
            r = await session.get(PipelineRun, run.id)
        assert r is not None and r.status == PipelineRunStatus.pending

    async def test_no_match_waiter_unchanged(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Payload mismatch → waiter untouched."""
        capturing = CapturingEventService()
        run, step, _ = await _insert_awaiting_run(
            session_factory,
            capturing,
            pipeline_name='no_match_pipe',
            event_type='approval.granted',
            match={'request_id': 'r1'},
        )

        await matcher_tick(
            event_type='approval.granted',
            routing_key='approval.granted',
            payload={'request_id': 'r2'},  # different
            correlation_id=None,
            causation_id=None,
            session_factory=session_factory,
            defs_provider=lambda: {},
            service_factory=_service_factory(capturing),
            log_service=NoOpLogService(),
        )

        async with session_factory() as session:
            r = await session.get(PipelineRun, run.id)
        assert r is not None and r.status == PipelineRunStatus.awaiting_event

    async def test_multi_waiter_fanout(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Single event resolves two waiters on the same event_type."""
        capturing = CapturingEventService()
        run1, step1, _ = await _insert_awaiting_run(
            session_factory,
            capturing,
            pipeline_name='fanout_pipe_1',
            event_type='shared.event',
            match={},
        )
        run2, step2, _ = await _insert_awaiting_run(
            session_factory,
            capturing,
            pipeline_name='fanout_pipe_2',
            event_type='shared.event',
            match={},
        )

        await matcher_tick(
            event_type='shared.event',
            routing_key='shared.event',
            payload={'any': 'value'},
            correlation_id=None,
            causation_id=None,
            session_factory=session_factory,
            defs_provider=lambda: {},
            service_factory=_service_factory(capturing),
            log_service=NoOpLogService(),
        )

        async with session_factory() as session:
            r1 = await session.get(PipelineRun, run1.id)
            r2 = await session.get(PipelineRun, run2.id)
        assert r1 is not None and r1.status == PipelineRunStatus.pending
        assert r2 is not None and r2.status == PipelineRunStatus.pending

    async def test_mq_trigger_start(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """MQ trigger fires → new PipelineRun created with trigger_source=mq."""
        capturing = CapturingEventService()
        defn = _make_defn('trigger_pipe', mq_routing_key='employee.created', mq_match={})

        await matcher_tick(
            event_type='employee.created',
            routing_key='employee.created',
            payload={'id': 'emp-1'},
            correlation_id='mq-cid',
            causation_id=None,
            session_factory=session_factory,
            defs_provider=lambda: {'trigger_pipe': defn},
            service_factory=_service_factory(capturing),
            log_service=NoOpLogService(),
        )

        async with session_factory() as session:
            result = await session.execute(
                sa.select(PipelineRun).where(
                    PipelineRun.pipeline_name == 'trigger_pipe',
                    PipelineRun.trigger_source == PipelineTriggerSource.mq,
                )
            )
            run = result.scalar_one_or_none()
        assert run is not None
        assert run.status == PipelineRunStatus.pending

    async def test_mq_trigger_duplicate_delivery(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Same event twice → only one PipelineRun (partial-UNIQUE dedupe)."""
        capturing = CapturingEventService()
        defn = _make_defn('dedup_pipe', mq_routing_key='emp.created', mq_match={})

        payload = {'id': 'emp-dedup'}
        for _ in range(2):
            await matcher_tick(
                event_type='emp.created',
                routing_key='emp.created',
                payload=payload,
                correlation_id=None,
                causation_id=None,
                session_factory=session_factory,
                defs_provider=lambda: {'dedup_pipe': defn},
                service_factory=_service_factory(capturing),
                log_service=NoOpLogService(),
            )

        async with session_factory() as session:
            result = await session.execute(
                sa.select(sa.func.count(PipelineRun.id)).where(
                    PipelineRun.pipeline_name == 'dedup_pipe',
                )
            )
            count = result.scalar_one()
        assert count == 1

    async def test_combined_event_waiter_and_trigger(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Event satisfies both a waiter and a trigger → both effects fire."""
        capturing = CapturingEventService()
        run, step, _ = await _insert_awaiting_run(
            session_factory,
            capturing,
            pipeline_name='combined_pipe',
            event_type='combined.event',
            match={},
        )
        defn = _make_defn('combined_trigger', mq_routing_key='combined.event', mq_match={})

        await matcher_tick(
            event_type='combined.event',
            routing_key='combined.event',
            payload={'data': 'x'},
            correlation_id=None,
            causation_id=None,
            session_factory=session_factory,
            defs_provider=lambda: {'combined_trigger': defn},
            service_factory=_service_factory(capturing),
            log_service=NoOpLogService(),
        )

        # Waiter resolved.
        async with session_factory() as session:
            r = await session.get(PipelineRun, run.id)
        assert r is not None and r.status == PipelineRunStatus.pending

        # MQ trigger fired.
        async with session_factory() as session:
            result = await session.execute(
                sa.select(PipelineRun).where(PipelineRun.pipeline_name == 'combined_trigger')
            )
            mq_run = result.scalar_one_or_none()
        assert mq_run is not None

    async def test_waiter_failure_does_not_block_trigger(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Stale waiter resolve failure → MQ trigger still fires.

        Inserts a waiter pointing to a non-existent step_run_id (bypassing FK
        via session_replication_role=replica, test-only) so that resolve_pipeline_event_waiter
        raises OrchestratorRowMissing.  The MQ trigger must still fire.
        """
        capturing = CapturingEventService()

        fake_step_id = uuid.uuid4()
        async with session_factory() as session:
            await session.execute(sa.text("SET session_replication_role = 'replica'"))
            await session.execute(
                sa.text(
                    'INSERT INTO pipeline_event_waiters (step_run_id, event_type, match, expires_at) '
                    "VALUES (:sid, 'isolate.event', '{}', :exp)"
                ),
                {'sid': fake_step_id, 'exp': _FUTURE},
            )
            await session.execute(sa.text("SET session_replication_role = 'origin'"))
            await session.commit()

        defn = _make_defn('isolate_trigger', mq_routing_key='isolate.event', mq_match={})

        await matcher_tick(
            event_type='isolate.event',
            routing_key='isolate.event',
            payload={},
            correlation_id=None,
            causation_id=None,
            session_factory=session_factory,
            defs_provider=lambda: {'isolate_trigger': defn},
            service_factory=_service_factory(capturing),
            log_service=NoOpLogService(),
        )

        # MQ trigger still fired despite waiter resolve failure.
        async with session_factory() as session:
            result = await session.execute(sa.select(PipelineRun).where(PipelineRun.pipeline_name == 'isolate_trigger'))
            mq_run = result.scalar_one_or_none()
        assert mq_run is not None


# ---------------------------------------------------------------------------
# Unit: loader args_from_payload validation (relocated from test_loader.py for
# clarity — tests _check_mq_trigger directly).
# ---------------------------------------------------------------------------


class TestLoaderArgsFromPayload:
    def test_valid_args_from_payload_accepted(self) -> None:
        from src.platform.orchestrator.loader import PipelineDefinitionLoader  # noqa: PLC0415

        loader = PipelineDefinitionLoader.__new__(PipelineDefinitionLoader)
        trigger = {
            'type': 'mq',
            'routing_key': 'test',
            'match': {},
            'args_from_payload': {'employee_id': 'data.id', 'name': 'data.name'},
        }
        # Should not raise.
        loader._check_mq_trigger(trigger, Path('test.yaml'))

    def test_invalid_arg_name_raises(self) -> None:
        from src.platform.orchestrator.loader import PipelineDefinitionLoader, PipelineTriggerError  # noqa: PLC0415

        loader = PipelineDefinitionLoader.__new__(PipelineDefinitionLoader)
        trigger = {
            'type': 'mq',
            'routing_key': 'test',
            'match': {},
            'args_from_payload': {'_bad_name': 'data.id'},
        }
        with pytest.raises(PipelineTriggerError, match='valid arg name'):
            loader._check_mq_trigger(trigger, Path('test.yaml'))

    def test_non_mapping_value_raises(self) -> None:
        from src.platform.orchestrator.loader import PipelineDefinitionLoader, PipelineTriggerError  # noqa: PLC0415

        loader = PipelineDefinitionLoader.__new__(PipelineDefinitionLoader)
        trigger = {
            'type': 'mq',
            'routing_key': 'test',
            'match': {},
            'args_from_payload': 'not_a_mapping',
        }
        with pytest.raises(PipelineTriggerError, match='must be a mapping'):
            loader._check_mq_trigger(trigger, Path('test.yaml'))
