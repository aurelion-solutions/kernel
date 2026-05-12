# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for the runner work loop.

Uses real PostgreSQL (session_factory from root conftest) and an in-memory
event sink.  A synthetic action is registered and torn down per-test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
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
from src.platform.orchestrator.runner import WorkerIdentity, run_one_iteration
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Pydantic schemas for synthetic action
# ---------------------------------------------------------------------------


class _SumArgs(BaseModel):
    a: int
    b: int


class _SumResult(BaseModel):
    total: int


# ---------------------------------------------------------------------------
# In-process pipeline loader backed by a dict
# ---------------------------------------------------------------------------


class _DictLoader:
    """Simple dict-backed pipeline lookup for integration tests."""

    def __init__(self, mapping: dict[tuple[str, int], PipelineDefinition]) -> None:
        self._mapping = mapping

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        return self._mapping.get((name, version))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker() -> WorkerIdentity:
    return WorkerIdentity(worker_id='integ-host-1-0', hostname='integ-host', pid=1, slot_index=0)


async def _insert_pending(
    session_factory: async_sessionmaker[AsyncSession],
    capturing: CapturingEventService,
    *,
    pipeline_name: str,
    pipeline_version: int = 1,
    args: dict | None = None,
) -> PipelineRun:
    async with session_factory() as session:
        svc = PipelineOrchestratorService(
            session=session,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            args=args or {},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='integ-corr',
        )
        await session.commit()
        return result.run


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestRunnerIntegration:
    async def test_full_run_completes_with_correct_event_sequence(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Full integration: create_pipeline_run → run_one_iteration → completed."""
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()

        @register_action('math', 'sum', _SumArgs, _SumResult)
        async def _sum(args: _SumArgs, ctx: ActionContext) -> dict:
            return {'total': args.a + args.b}

        try:
            # Write a fixture YAML pipeline (no schema.json validation in loader
            # without schema; instead we build PipelineDefinition directly).
            defn = PipelineDefinition(
                name='math_pipe',
                version=1,
                schema_version=1,
                source_path=tmp_path / 'math_pipe.yaml',
                content_hash='integ_hash_001',
                args_schema_dict={'type': 'object', 'properties': {'a': {'type': 'integer'}, 'b': {'type': 'integer'}}},
                triggers=(),
                steps=(
                    {
                        'name': 'add_step',
                        'kind': 'engine_call',
                        'engine': 'math',
                        'action': 'sum',
                        'args': {'a': '${args.a}', 'b': '${args.b}'},
                    },
                ),
                raw_dict={},
            )
            loader = _DictLoader({('math_pipe', 1): defn})
            run = await _insert_pending(
                session_factory,
                capturing,
                pipeline_name='math_pipe',
                args={'a': 3, 'b': 4},
            )
            capturing.clear()

            outcome = await run_one_iteration(
                session_factory,
                worker=_make_worker(),
                pipeline_loader=loader,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
            )
            assert outcome == 'completed'

            # Verify DB: run completed.
            async with session_factory() as session:
                row = await session.execute(
                    sa.select(PipelineRun).where(PipelineRun.id == run.id).execution_options(populate_existing=True)
                )
                final_run = row.scalar_one()

            assert final_run.status == PipelineRunStatus.completed
            assert final_run.finished_at is not None

            # Verify event sequence: started → step.started → step.completed → completed.
            event_types = [e.event_type for e in capturing.emitted]
            assert event_types == [
                'pipeline.run.started',
                'pipeline.step.started',
                'pipeline.step.completed',
                'pipeline.run.completed',
            ]

            # Verify step result in step.completed event.
            step_completed = capturing.filter_by_type('pipeline.step.completed')
            assert len(step_completed) == 1
            assert step_completed[0].payload['result'] == {'total': 7}

        finally:
            ACTION_REGISTRY._clear_for_tests()

    async def test_run_with_step_that_fails(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Action that raises → step failed + run failed events emitted."""
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()

        class _BoomArgs(BaseModel):
            reason: str

        class _BoomResult(BaseModel):
            pass

        @register_action('test_integ', 'boom', _BoomArgs, _BoomResult)
        async def _boom(args: _BoomArgs, ctx: ActionContext) -> dict:
            raise ValueError(args.reason)

        try:
            defn = PipelineDefinition(
                name='boom_pipe',
                version=1,
                schema_version=1,
                source_path=tmp_path / 'boom_pipe.yaml',
                content_hash='integ_hash_002',
                args_schema_dict={},
                triggers=(),
                steps=(
                    {
                        'name': 'fail_step',
                        'kind': 'engine_call',
                        'engine': 'test_integ',
                        'action': 'boom',
                        'args': {'reason': 'deliberate'},
                    },
                ),
                raw_dict={},
            )
            loader = _DictLoader({('boom_pipe', 1): defn})
            run = await _insert_pending(
                session_factory,
                capturing,
                pipeline_name='boom_pipe',
            )
            capturing.clear()

            outcome = await run_one_iteration(
                session_factory,
                worker=_make_worker(),
                pipeline_loader=loader,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
            )
            assert outcome == 'failed'

            # Verify DB: run failed.
            async with session_factory() as session:
                row = await session.execute(
                    sa.select(PipelineRun).where(PipelineRun.id == run.id).execution_options(populate_existing=True)
                )
                final_run = row.scalar_one()

            assert final_run.status == PipelineRunStatus.failed

            # Events: started, step.started, step.failed, run.failed.
            event_types = [e.event_type for e in capturing.emitted]
            assert 'pipeline.run.started' in event_types
            assert 'pipeline.step.started' in event_types
            assert 'pipeline.step.failed' in event_types
            assert 'pipeline.run.failed' in event_types

        finally:
            ACTION_REGISTRY._clear_for_tests()


class TestWaitForEventIntegration:
    async def test_wait_for_event_db_integration(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Real Postgres: wait_for_event step → all three rows reflect awaiting_event."""
        capturing = CapturingEventService()

        defn = PipelineDefinition(
            name='wait_pipe',
            version=1,
            schema_version=1,
            source_path=tmp_path / 'wait_pipe.yaml',
            content_hash='wait_integ_hash_001',
            args_schema_dict={},
            triggers=(),
            steps=(
                {
                    'name': 'wait_step',
                    'type': 'wait_for_event',
                    'event': 'employee.hired',
                    'match': {'department': 'engineering'},
                    'timeout': '1h',
                    'on_timeout': 'fail',
                },
            ),
            raw_dict={},
        )
        loader = _DictLoader({('wait_pipe', 1): defn})
        run = await _insert_pending(
            session_factory,
            capturing,
            pipeline_name='wait_pipe',
        )
        capturing.clear()

        outcome = await run_one_iteration(
            session_factory,
            worker=_make_worker(),
            pipeline_loader=loader,
            events=EventService(sink=capturing),
            logs=NoOpLogService(),
        )
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

        assert final_run.status == PipelineRunStatus.awaiting_event
        assert step_run.status == StepRunStatus.awaiting_event
        assert waiter.event_type == 'employee.hired'
        assert waiter.match == {'department': 'engineering'}
        assert waiter.expires_at is not None


class TestHeartbeatRefreshIntegration:
    async def test_run_one_iteration_refreshes_heartbeat_during_action(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Action sleeps 4s → heartbeat is refreshed ≥2 distinct times during execution."""
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()

        class _SleepArgs(BaseModel):
            seconds: float

        class _SleepResult(BaseModel):
            done: bool

        @register_action('test_hb', 'sleep', _SleepArgs, _SleepResult)
        async def _sleep_action(args: _SleepArgs, ctx: ActionContext) -> dict:
            await asyncio.sleep(args.seconds)
            return {'done': True}

        try:
            defn = PipelineDefinition(
                name='hb_integ_pipe',
                version=1,
                schema_version=1,
                source_path=tmp_path / 'hb_integ_pipe.yaml',
                content_hash='hb_integ_hash_001',
                args_schema_dict={},
                triggers=(),
                steps=(
                    {
                        'name': 'sleep_step',
                        'kind': 'engine_call',
                        'engine': 'test_hb',
                        'action': 'sleep',
                        'args': {'seconds': 4.0},
                    },
                ),
                raw_dict={},
            )
            loader = _DictLoader({('hb_integ_pipe', 1): defn})
            run = await _insert_pending(
                session_factory,
                capturing,
                pipeline_name='hb_integ_pipe',
            )
            capturing.clear()

            # Sample last_heartbeat_at from a side session while run_one_iteration runs.
            observed: list[object] = []

            async def _sampler() -> None:
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    async with session_factory() as s:
                        row_result = await s.execute(
                            sa.select(PipelineRun.last_heartbeat_at).where(PipelineRun.id == run.id)
                        )
                        val = row_result.scalar_one_or_none()
                    if val is not None:
                        observed.append(val)

            sampler_task = asyncio.create_task(_sampler())

            outcome = await run_one_iteration(
                session_factory,
                worker=_make_worker(),
                pipeline_loader=loader,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
            )
            sampler_task.cancel()
            try:
                await sampler_task
            except asyncio.CancelledError:
                pass

            assert outcome == 'completed'
            distinct_values = set(observed)
            assert len(distinct_values) >= 2, (
                f'Expected ≥2 distinct heartbeat timestamps, got {len(distinct_values)}: {distinct_values}'
            )

        finally:
            ACTION_REGISTRY._clear_for_tests()

    async def test_run_one_iteration_stops_refresher_on_step_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """Action raises immediately → run returns 'failed', no leaked tasks."""
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()

        class _BoomArgs(BaseModel):
            pass

        class _BoomResult(BaseModel):
            pass

        @register_action('test_hb_fail', 'boom', _BoomArgs, _BoomResult)
        async def _boom(args: _BoomArgs, ctx: ActionContext) -> dict:
            raise RuntimeError('immediate failure')

        try:
            defn = PipelineDefinition(
                name='hb_fail_pipe',
                version=1,
                schema_version=1,
                source_path=tmp_path / 'hb_fail_pipe.yaml',
                content_hash='hb_fail_hash_001',
                args_schema_dict={},
                triggers=(),
                steps=(
                    {
                        'name': 'boom_step',
                        'kind': 'engine_call',
                        'engine': 'test_hb_fail',
                        'action': 'boom',
                        'args': {},
                    },
                ),
                raw_dict={},
            )
            loader = _DictLoader({('hb_fail_pipe', 1): defn})
            await _insert_pending(session_factory, capturing, pipeline_name='hb_fail_pipe')
            capturing.clear()

            tasks_before = set(asyncio.all_tasks())

            outcome = await run_one_iteration(
                session_factory,
                worker=_make_worker(),
                pipeline_loader=loader,
                events=EventService(sink=capturing),
                logs=NoOpLogService(),
            )

            # Give event loop one cycle to settle any pending cleanup.
            await asyncio.sleep(0)

            tasks_after = set(asyncio.all_tasks())
            leaked = tasks_after - tasks_before
            assert outcome == 'failed'
            assert not leaked, f'Leaked tasks after failed run: {leaked}'

        finally:
            ACTION_REGISTRY._clear_for_tests()
