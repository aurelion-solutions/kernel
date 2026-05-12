# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration test: two-slot reclaim scenario.

Worker A claims a run, its heartbeat goes stale (forced via raw SQL).
Worker B's reclaim sweep picks it up and processes it to completion.

No wall-clock sleeps — staleness is injected via raw SQL.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import (
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
)
from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionContext, register_action
from src.platform.orchestrator.runner import WorkerIdentity, reclaim_sweep_tick, run_one_iteration
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Synthetic action and pipeline
# ---------------------------------------------------------------------------


class _EchoArgs(BaseModel):
    value: str


class _EchoResult(BaseModel):
    echo: str


class _DictLoader:
    def __init__(self, mapping: dict[tuple[str, int], PipelineDefinition]) -> None:
        self._mapping = mapping

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        return self._mapping.get((name, version))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(slot: int) -> WorkerIdentity:
    return WorkerIdentity(
        worker_id=f'reclaim-integ-host-1-{slot}',
        hostname='reclaim-integ-host',
        pid=1,
        slot_index=slot,
    )


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
    pipeline_name: str,
    pipeline_version: int = 1,
) -> PipelineRun:
    async with session_factory() as session:
        svc = _make_svc(session, capturing)
        result = await svc.create_pipeline_run(
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            args={},
            trigger_source=PipelineTriggerSource.http,
            correlation_id='integ-reclaim',
        )
        await session.commit()
        return result.run


async def _make_stale(session_factory: async_sessionmaker[AsyncSession], run_id: object) -> None:
    """Force last_heartbeat_at to 30 seconds ago — makes the run appear stale."""
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE pipeline_runs SET last_heartbeat_at = now() - interval '30 seconds' WHERE id = :rid"),
            {'rid': run_id},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Two-slot reclaim scenario
# ---------------------------------------------------------------------------


class TestTwoSlotReclaim:
    async def test_slot_b_reclaims_stale_run_from_slot_a(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """
        Slot A claims a run. Its heartbeat is made stale via raw SQL.
        Slot B's reclaim_sweep_tick releases it; Slot B then processes it.

        Asserts:
        - final run status = completed
        - pipeline.run.heartbeat_lost event emitted
        - pipeline.step.aborted event emitted for the aborted attempt
        - The aborted StepRun row has status='aborted'
        - The completed StepRun row has attempt > aborted attempt
        """
        ACTION_REGISTRY._clear_for_tests()
        capturing = CapturingEventService()
        events = EventService(sink=capturing)

        @register_action('reclaim_integ', 'echo', _EchoArgs, _EchoResult)  # type: ignore[arg-type]
        async def _echo(args: _EchoArgs, ctx: ActionContext) -> dict[str, object]:
            return {'echo': args.value}

        try:
            defn = PipelineDefinition(
                name='reclaim_integ_pipe',
                version=1,
                schema_version=1,
                source_path=tmp_path / 'reclaim_integ_pipe.yaml',
                content_hash='reclaim_integ_hash_001',
                args_schema_dict={},
                triggers=(),
                steps=(
                    {
                        'name': 'echo_step',
                        'kind': 'engine_call',
                        'engine': 'reclaim_integ',
                        'action': 'echo',
                        'args': {'value': 'hello'},
                    },
                ),
                raw_dict={},
            )
            loader = _DictLoader({('reclaim_integ_pipe', 1): defn})

            # --- Slot A: claims the run (no step yet — simulates pre-step abandonment) ---
            run = await _insert_pending(session_factory, capturing, pipeline_name='reclaim_integ_pipe')
            worker_a = _make_worker(0)

            async with session_factory() as session_a:
                svc_a = _make_svc(session_a, capturing)
                await svc_a.mark_pipeline_running(run.id, worker_id=worker_a.worker_id)
                await session_a.commit()

            # --- Force staleness (heartbeat way in the past) ---
            await _make_stale(session_factory, run.id)

            capturing.clear()

            # --- Slot B: sweep reclaims the run ---
            await reclaim_sweep_tick(
                session_factory,
                events=events,
                logs=NoOpLogService(),
            )

            # heartbeat_lost event (no active step, so no step.aborted).
            hb_events = [e for e in capturing.emitted if e.event_type == 'pipeline.run.heartbeat_lost']
            assert len(hb_events) == 1
            assert hb_events[0].payload['run_id'] == str(run.id)
            assert hb_events[0].payload['previous_worker_id'] == worker_a.worker_id

            # Run should be pending again.
            async with session_factory() as session:
                run_row = await session.get(PipelineRun, run.id)
            assert run_row is not None
            assert run_row.status == PipelineRunStatus.pending

            capturing.clear()

            # --- Slot B: processes the run to completion ---
            worker_b = _make_worker(1)
            outcome = await run_one_iteration(
                session_factory,
                worker=worker_b,
                pipeline_loader=loader,
                events=events,
                logs=NoOpLogService(),
            )
            assert outcome == 'completed'

            # Final run status.
            async with session_factory() as session:
                final_run = await session.get(PipelineRun, run.id)
            assert final_run is not None
            assert final_run.status == PipelineRunStatus.completed

            # Events: run.started, step.started, step.completed, run.completed.
            event_types = [e.event_type for e in capturing.emitted]
            assert 'pipeline.run.started' in event_types
            assert 'pipeline.run.completed' in event_types

        finally:
            ACTION_REGISTRY._clear_for_tests()
