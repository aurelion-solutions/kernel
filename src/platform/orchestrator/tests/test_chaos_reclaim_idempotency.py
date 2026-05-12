# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Chaos test: crash-and-resume preserves effective_grants row count.

Invariant: ``count(effective_grants)`` after crash-and-resume equals
``count(effective_grants)`` after a clean run for the same
``effective_access.project_application`` action.

This exercises the idempotency guarantee introduced in Phase 18 Step 21
(ARCH_CONTEXT §353–§359, engine-action idempotency).  The pipeline uses
UPSERT into effective_grants, so a second projection on the same
``application_id`` must produce the same set of rows — no duplicates, no
missing grants.

Crash is synthesised via raw-SQL heartbeat backdating (same pattern as
``test_reclaim_integration.py``) — no real process kill, no asyncio
cancellation.
"""

from __future__ import annotations

from datetime import UTC, datetime
import importlib
import sys
import uuid
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.engines.effective_access.models import EffectiveGrant
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.loader import PipelineDefinition
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus, PipelineTriggerSource
from src.platform.orchestrator.registry import ACTION_REGISTRY
from src.platform.orchestrator.runner import WorkerIdentity, reclaim_sweep_tick, run_one_iteration
from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ACTIONS_MODULE = 'src.engines.effective_access.actions'
_NOW = datetime(2026, 1, 1, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Helpers copied from test_reclaim_integration.py (self-contained; importing
# private symbols across the inventory/orchestrator slice boundary is forbidden).
# ---------------------------------------------------------------------------


class _DictLoader:
    def __init__(self, mapping: dict[tuple[str, int], PipelineDefinition]) -> None:
        self._mapping = mapping

    def get(self, name: str, version: int) -> PipelineDefinition | None:
        return self._mapping.get((name, version))


def _make_worker(slot: int) -> WorkerIdentity:
    return WorkerIdentity(
        worker_id=f'chaos-step-23-host-1-{slot}',
        hostname='chaos-step-23-host',
        pid=1,
        slot_index=slot,
    )


async def _make_stale(session_factory: async_sessionmaker[AsyncSession], run_id: object) -> None:
    """Force last_heartbeat_at to 30 seconds ago — makes the run appear stale."""
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE pipeline_runs SET last_heartbeat_at = now() - interval '30 seconds' WHERE id = :rid"),
            {'rid': run_id},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# DB seed helpers (copied from test_actions_projection.py:93-188).
# Not imported because that module lives in the engines/ slice and importing
# private test helpers across slice boundaries is explicitly prohibited.
# ---------------------------------------------------------------------------


async def _make_employee_subject(session: AsyncSession) -> UUID:
    from src.inventory.employees.repository import create_employee  # noqa: PLC0415
    from src.inventory.persons.repository import create_person  # noqa: PLC0415
    from src.inventory.subjects.models import Subject  # noqa: PLC0415

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_app_and_resource(session: AsyncSession) -> tuple[UUID, UUID]:
    from src.inventory.resources.models import Resource  # noqa: PLC0415
    from src.platform.applications.models import Application  # noqa: PLC0415

    app = Application(
        name=f'chaos-step-23-app-{uuid.uuid4()}',
        code=f'cs23-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    res_ext = str(uuid.uuid4())
    resource = Resource(
        external_id=res_ext,
        application_id=app.id,
        kind='database',
        resource_type='database',
        resource_key=res_ext,
    )
    session.add(resource)
    await session.flush()
    return app.id, resource.id


async def _seed_access_fact_in_shim(
    session: AsyncSession,
    subject_id: UUID,
    resource_id: UUID,
) -> UUID:
    fact_id = uuid.uuid4()
    row = await session.execute(sa.text("SELECT id FROM ref_actions WHERE slug = 'read'"))
    action_id = row.scalar_one()
    await session.execute(
        sa.text(
            'INSERT INTO access_facts '
            '(id, subject_id, resource_id, action_id, effect, valid_from, observed_at) '
            "VALUES (:id, :sid, :rid, :aid, 'allow', :vf, :oa)"
        ),
        {
            'id': fact_id,
            'sid': subject_id,
            'rid': resource_id,
            'aid': action_id,
            'vf': _NOW,
            'oa': _NOW,
        },
    )
    await session.flush()
    return fact_id


async def _make_initiative(session: AsyncSession, access_fact_id: UUID) -> UUID:
    from src.inventory.initiatives.models import Initiative  # noqa: PLC0415

    init = Initiative(
        access_fact_id=access_fact_id,
        type=InitiativeType.birthright,
        origin='chaos-step-23-origin',
        valid_from=_NOW,
    )
    session.add(init)
    await session.flush()
    return init.id


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


class TestChaosReclaimIdempotency:
    async def test_resumed_run_produces_same_row_count_as_clean_run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """Crash-and-resume via reclaim sweep produces same effective_grants count.

        Phase A: clean pipeline run → record n_clean rows.
        Phase B: run is claimed, heartbeat made stale, reclaim releases it,
                 worker B resumes → record n_resumed rows.
        Invariant: n_resumed == n_clean and n_clean >= 1.
        Also asserts no duplicate rows (natural-key UPSERT sanity).
        """
        ACTION_REGISTRY._clear_for_tests()
        sys.modules.pop(_ACTIONS_MODULE, None)
        importlib.import_module(_ACTIONS_MODULE)

        capturing = CapturingEventService()
        events = EventService(sink=capturing)

        app_id: UUID | None = None

        try:
            # ------------------------------------------------------------------
            # Seed
            # ------------------------------------------------------------------
            async with session_factory() as seed_session:
                subject_id = await _make_employee_subject(seed_session)
                app_id, resource_id = await _make_app_and_resource(seed_session)
                fact_id = await _seed_access_fact_in_shim(seed_session, subject_id, resource_id)
                await _make_initiative(seed_session, fact_id)
                await seed_session.commit()

            # Stable step-args dict — identical for Phase A and Phase B so that
            # content_hash is the same for both runs (Q2 from TASK.md §3).
            step_args: dict[str, object] = {
                'application_id': str(app_id),
                'now': _NOW.isoformat(),
                'correlation_id': str(uuid.uuid4()),
            }

            defn = PipelineDefinition(
                name='chaos_step_23_pipe',
                version=1,
                schema_version=1,
                source_path=None,  # type: ignore[arg-type]
                content_hash='chaos_step_23_hash_001',
                args_schema_dict={},
                triggers=(),
                steps=(
                    {
                        'name': 'project_app_step',
                        'kind': 'engine_call',
                        'engine': 'effective_access',
                        'action': 'project_application',
                        'args': step_args,
                    },
                ),
                raw_dict={},
            )
            loader = _DictLoader({('chaos_step_23_pipe', 1): defn})

            # ------------------------------------------------------------------
            # Phase A — clean run
            # ------------------------------------------------------------------
            async with session_factory() as session_a:
                svc_a = PipelineOrchestratorService(
                    session=session_a,
                    events=events,
                    logs=NoOpLogService(),
                )
                result_a = await svc_a.create_pipeline_run(
                    pipeline_name='chaos_step_23_pipe',
                    pipeline_version=1,
                    args=step_args,  # type: ignore[arg-type]
                    trigger_source=PipelineTriggerSource.http,
                    correlation_id='chaos-step-23-a',
                )
                await session_a.commit()
            run_a_id: UUID = result_a.run.id

            outcome_a = await run_one_iteration(
                session_factory,
                worker=_make_worker(0),
                pipeline_loader=loader,
                events=events,
                logs=NoOpLogService(),
            )
            assert outcome_a == 'completed', f'Phase A run did not complete: {outcome_a!r}'

            async with session_factory() as chk_session:
                run_a = await chk_session.get(PipelineRun, run_a_id)
            assert run_a is not None
            assert run_a.status == PipelineRunStatus.completed

            # Guardian recommendation: at least one pipeline.run.completed event for run A
            completed_events_a = [e for e in capturing.emitted if e.event_type == 'pipeline.run.completed']
            assert len(completed_events_a) > 0, 'Expected pipeline.run.completed event for run A'

            async with session_factory() as cnt_session:
                n_clean_row = await cnt_session.execute(
                    sa.select(sa.func.count()).where(
                        EffectiveGrant.application_id == app_id,
                    )
                )
                n_clean: int = n_clean_row.scalar_one()

            # Riskiest-assumption guard (TASK.md §9): seed must produce at least 1 row.
            assert n_clean >= 1, (
                f'Clean run produced 0 effective_grants rows — seed is incomplete. application_id={app_id}'
            )

            # ------------------------------------------------------------------
            # Reset projection between phases
            # ------------------------------------------------------------------
            async with session_factory() as del_session:
                await del_session.execute(sa.delete(EffectiveGrant).where(EffectiveGrant.application_id == app_id))
                await del_session.commit()

            # ------------------------------------------------------------------
            # Phase B — crash-and-resume
            # ------------------------------------------------------------------
            capturing.clear()

            async with session_factory() as session_b:
                svc_b = PipelineOrchestratorService(
                    session=session_b,
                    events=events,
                    logs=NoOpLogService(),
                )
                result_b = await svc_b.create_pipeline_run(
                    pipeline_name='chaos_step_23_pipe',
                    pipeline_version=1,
                    args=step_args,  # type: ignore[arg-type]
                    trigger_source=PipelineTriggerSource.http,
                    correlation_id='chaos-step-23-b',
                )
                await session_b.commit()
            # Guardian recommendation: run B must be a fresh insert (partial UNIQUE
            # allows this because run A is now in terminal status — Q1 from TASK.md §3).
            assert result_b.created is True, 'Phase B create_pipeline_run must insert a fresh row'
            run_b_id: UUID = result_b.run.id

            # Worker A claims the run but does NOT process any step — simulates
            # pre-step abandonment (mirrors test_reclaim_integration.py:158-165).
            async with session_factory() as claim_session:
                svc_claim = PipelineOrchestratorService(
                    session=claim_session,
                    events=events,
                    logs=NoOpLogService(),
                )
                await svc_claim.mark_pipeline_running(
                    run_b_id,
                    worker_id=_make_worker(0).worker_id,
                )
                await claim_session.commit()

            # Synthesise crash: backdate heartbeat so the reclaim sweep sees a stale run.
            await _make_stale(session_factory, run_b_id)

            # Reclaim sweep releases the stale run back to pending.
            await reclaim_sweep_tick(
                session_factory,
                events=events,
                logs=NoOpLogService(),
            )

            # Assert heartbeat_lost event emitted for run B (Q3 verification).
            hb_events = [
                e
                for e in capturing.emitted
                if e.event_type == 'pipeline.run.heartbeat_lost' and e.payload.get('run_id') == str(run_b_id)
            ]
            assert len(hb_events) == 1, f'Expected exactly 1 heartbeat_lost for run B, got {len(hb_events)}'

            # No step.aborted because no StepRun was created (pre-step abandonment).
            aborted_events = [e for e in capturing.emitted if e.event_type == 'pipeline.step.aborted']
            assert len(aborted_events) == 0, 'No step.aborted expected (pre-step abandonment)'

            async with session_factory() as status_session:
                run_b_pending = await status_session.get(PipelineRun, run_b_id)
            assert run_b_pending is not None
            assert run_b_pending.status == PipelineRunStatus.pending

            capturing.clear()

            # Worker B picks up the released run and runs to completion.
            outcome_b = await run_one_iteration(
                session_factory,
                worker=_make_worker(1),
                pipeline_loader=loader,
                events=events,
                logs=NoOpLogService(),
            )
            assert outcome_b == 'completed', f'Phase B resumed run did not complete: {outcome_b!r}'

            async with session_factory() as final_session:
                run_b_final = await final_session.get(PipelineRun, run_b_id)
            assert run_b_final is not None
            assert run_b_final.status == PipelineRunStatus.completed

            # Guardian recommendation: at least one step_run completed for run B.
            from src.platform.orchestrator.models import StepRun, StepRunStatus  # noqa: PLC0415

            async with session_factory() as sr_session:
                sr_result = await sr_session.execute(
                    sa.select(StepRun).where(
                        StepRun.pipeline_run_id == run_b_id,
                        StepRun.status == StepRunStatus.completed,
                    )
                )
                completed_steps = sr_result.scalars().all()
            assert len(completed_steps) >= 1, 'Expected at least one completed step_run for run B'

            # Guardian recommendation: capturing must contain at least one event for run B
            # and pipeline.run.completed must be among them.
            assert len(capturing.emitted) > 0, 'Expected events for run B worker iteration'
            completed_b = [e for e in capturing.emitted if e.event_type == 'pipeline.run.completed']
            assert len(completed_b) > 0, 'Expected pipeline.run.completed event for run B'

            # ------------------------------------------------------------------
            # Phase C — invariant
            # ------------------------------------------------------------------
            async with session_factory() as inv_session:
                n_resumed_row = await inv_session.execute(
                    sa.select(sa.func.count()).where(
                        EffectiveGrant.application_id == app_id,
                    )
                )
                n_resumed: int = n_resumed_row.scalar_one()

            assert n_resumed == n_clean, (
                f'Crash-and-resume changed effective_grants count: '
                f'clean={n_clean} resumed={n_resumed} application_id={app_id}'
            )

            # UPSERT natural-key sanity: no duplicate (subject, resource, action, initiative) tuples.
            # Note: the column is named "action" (enum), not "action_id".
            async with session_factory() as dup_session:
                dup_result = await dup_session.execute(
                    sa.text(
                        'SELECT subject_id, resource_id, action, source_initiative_id, count(*) '
                        'FROM effective_grants '
                        'WHERE application_id = :app_id '
                        'GROUP BY 1,2,3,4 HAVING count(*) > 1'
                    ),
                    {'app_id': app_id},
                )
                duplicates = dup_result.fetchall()
            assert duplicates == [], f'Duplicate effective_grants rows detected: {duplicates}'

        finally:
            # Cleanup: remove all data created by this test.
            if app_id is not None:
                try:
                    async with session_factory() as cleanup_session:
                        await cleanup_session.execute(
                            sa.delete(EffectiveGrant).where(EffectiveGrant.application_id == app_id)
                        )
                        await cleanup_session.commit()
                except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                    pass

            try:
                async with session_factory() as cleanup_session2:
                    await cleanup_session2.execute(sa.text('DELETE FROM step_runs'))
                    await cleanup_session2.execute(sa.text('DELETE FROM pipeline_runs'))
                    await cleanup_session2.commit()
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                pass

            ACTION_REGISTRY._clear_for_tests()
