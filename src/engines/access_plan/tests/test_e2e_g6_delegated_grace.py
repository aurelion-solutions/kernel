# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""E2E test — Phase 19 Step G6: delegated + grace initiatives.

Two parallel scenarios:

--- DELEGATED scenario ---
1. Employee A exists (delegator), Employee B exists (delegatee).
2. A seeds a delegated Initiative for B on resource X:
   type=delegated, origin='delegation:<A_subject_ref>', subject_ref=B_subject_ref.
   This simulates A having previously delegated access to B (the delegation record
   is the Initiative row itself — no separate delegation table in Phase 19).
3. POST /plans for B → PDP carry-over picks up the delegated initiative →
   plan contains add_fact item with delegated initiative.
4. Verify: PlanItem.initiatives list contains entry with
   type='delegated', origin='delegation:<A_subject_ref>'.
5. execute_plan: grant items done; InitiativeService.create_or_get called;
   resulting Initiative in DB has origin containing 'delegation:'.

--- GRACE scenario ---
1. Employee C exists with an existing 'requested' Initiative that has been closed
   (valid_until set to the past), simulating a revoke.
2. Seed a grace Initiative for C on resource Y:
   type=grace, origin='grace:<original_requested_id>', subject_ref=C_subject_ref,
   valid_until=now() + 7 days.
3. POST /plans for C → PDP carry-over picks up the grace initiative (not expired) →
   plan contains add_fact item with grace initiative.
4. Verify: PlanItem.initiatives contains entry with type='grace',
   origin='grace:<original_requested_id>'.
5. execute_plan: grant items done; resulting Initiative in DB has origin 'grace:...'.
6. Initiative chain via origin: grace Initiative origin points to the closed
   requested Initiative id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.engines.access_apply.execute_plan import execute_plan
from src.engines.access_plan.deps import (
    get_access_plan_service,
    get_plan_orchestrator_service,
    get_plan_pipelines,
)
from src.engines.access_plan.models import (
    AccessApplyActive,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
)
from src.engines.access_plan.service import AccessPlanService
from src.engines.policy_assessment.generative.schemas import CurrentInitiative
from src.engines.policy_assessment.generative.service import GenerativePDPService
from src.engines.policy_assessment.schemas import RulePack
import src.inventory.employees.models  # noqa: F401 — registers Employee for create_all
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.employees.repository import create_employee
import src.inventory.initiatives.models  # noqa: F401 — registers Initiative for create_all
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.initiatives.service import InitiativeService
from src.inventory.org_units.models import OrgUnit
import src.inventory.persons.models  # noqa: F401 — registers Person for create_all
from src.inventory.persons.repository import create_person
from src.inventory.subjects.models import Subject, SubjectKind
import src.platform.applications.models  # noqa: F401 — registers Application for create_all
import src.platform.connectors.models  # noqa: F401 — registers ConnectorInstance for create_all
from src.platform.connectors.models import ConnectorInstance
from src.platform.events.service import noop_event_service
from src.platform.logs.service import noop_log_service
from src.platform.orchestrator.loader import PipelineDefinition
import src.platform.orchestrator.models  # noqa: F401 — registers PipelineRun for create_all
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus, PipelineTriggerSource
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.orchestrator.service_types import PipelineRunCreateResult
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# Policy fixtures
# ---------------------------------------------------------------------------

# Delegated scenario: no birthright rules — the only access B gets is from
# carry-over of the delegated initiative seeded directly in DB.
_DELEGATED_APP_ID = 'mock-app-g6-delegated'

_DELEGATED_RULES = RulePack(lifecycle=[], risk=[])

# Grace scenario: no birthright rules — access C gets is from carry-over of
# the grace initiative seeded directly in DB.
_GRACE_APP_ID = 'mock-app-g6-grace'

_GRACE_RULES = RulePack(lifecycle=[], risk=[])


# ---------------------------------------------------------------------------
# Stub connector — preflight mismatch → connector → post-verify match
# ---------------------------------------------------------------------------


class _G6StubConnector:
    """Stub connector for G6: mismatch → connector call → match sequence."""

    def __init__(self, n_items: int = 1) -> None:
        self.invoke_calls: list[dict[str, Any]] = []
        # Each item needs 2 verify_fact calls: preflight(mismatch) + post(match)
        self._verify_seq = ['mismatch', 'match'] * n_items
        self._verify_idx = 0

    async def invoke(
        self,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.invoke_calls.append({'operation': operation, 'payload': payload})
        if operation == 'verify_fact':
            if self._verify_idx < len(self._verify_seq):
                result = self._verify_seq[self._verify_idx]
                self._verify_idx += 1
            else:
                result = 'match'
            return {'status': 'ok', 'result': result}
        return {'status': 'ok'}


# ---------------------------------------------------------------------------
# Stub sync service
# ---------------------------------------------------------------------------


class _G6StubSyncService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def sync_single_fact(  # type: ignore[override]
        self,
        descriptor: Any,
        op: Any,
        event_key: str,
        *,
        subject_id: Any = None,
        resource_id: Any = None,
        action_id: str = '',
        application_id_denorm: str = '',
        subject_kind_denorm: str = '',
        account_id: Any = None,
        correlation_id: str = '',
    ) -> bool:
        self.calls.append({'op': op, 'event_key': event_key, 'application_id_denorm': application_id_denorm})
        return True


# ---------------------------------------------------------------------------
# Fake pipeline definition
# ---------------------------------------------------------------------------


def _fake_pipeline_def() -> PipelineDefinition:
    return PipelineDefinition(
        name='access_apply_pipeline',
        version=1,
        schema_version=1,
        source_path=Path('/fake/access_apply_pipeline.yaml'),
        content_hash='g6_hash',
        args_schema_dict={},
        steps=(),
        triggers=(),
        raw_dict={},
    )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_org_unit(session: AsyncSession, ext_id: str) -> OrgUnit:
    org = OrgUnit(external_id=ext_id, name=f'G6 Org {ext_id}')
    session.add(org)
    await session.flush()
    return org


async def _seed_employee_with_subject(
    session: AsyncSession,
    org_unit_id: uuid.UUID,
    name_suffix: str,
    *,
    employment_status: str = 'active',
) -> tuple[Employee, Subject]:
    """Create Person → Employee + EmployeeAttribute + Subject.  Returns (emp, subj)."""
    person = await create_person(
        session,
        external_id=f'emp-g6-{name_suffix}-{uuid.uuid4().hex[:6]}',
        full_name=f'G6 Employee {name_suffix}',
    )
    await session.flush()

    emp = await create_employee(session, person_id=person.id)
    emp.org_unit_id = org_unit_id
    await session.flush()

    attr = EmployeeAttribute(employee_id=emp.id, key='employment_status', value=employment_status)
    session.add(attr)

    subj = Subject(
        external_id=f'subj-g6-{name_suffix}-{uuid.uuid4().hex[:6]}',
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return emp, subj


async def _seed_connector(session: AsyncSession, instance_id: str) -> ConnectorInstance:
    from src.platform.connectors.mock_connector import MOCK_CONNECTOR_DESCRIPTOR  # noqa: PLC0415

    conn = ConnectorInstance(
        instance_id=instance_id,
        tags=['mock', 'g6'],
        descriptor=MOCK_CONNECTOR_DESCRIPTOR.model_dump(mode='json'),
    )
    session.add(conn)
    await session.flush()
    return conn


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    orchestrator: PipelineOrchestratorService,
    pipelines: dict[str, Any],
    rule_pack: RulePack,
) -> FastAPI:
    pdp = GenerativePDPService(rule_pack=rule_pack)
    settings = RuntimeSettingsConfig()

    async def override_get_db():  # type: ignore[return]
        async with session_factory() as s:
            try:
                yield s
                await s.commit()
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                await s.rollback()
                raise

    def override_get_service(  # type: ignore[return]
        session: AsyncSession = Depends(get_db),  # noqa: B008
    ) -> AccessPlanService:
        return AccessPlanService(
            session=session,
            pdp_service=pdp,
            event_service=noop_event_service,
            settings=settings,
        )

    async def override_get_orchestrator() -> PipelineOrchestratorService:
        return orchestrator

    def override_get_pipelines() -> dict[str, Any]:
        return pipelines

    from src.engines.access_plan.routes import router as plan_router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(plan_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_access_plan_service] = override_get_service
    app.dependency_overrides[get_plan_orchestrator_service] = override_get_orchestrator
    app.dependency_overrides[get_plan_pipelines] = override_get_pipelines
    return app


# ---------------------------------------------------------------------------
# Helper: build fake orchestrator + pipeline run
# ---------------------------------------------------------------------------


def _make_orchestrator_and_run(run_id: uuid.UUID) -> tuple[AsyncMock, MagicMock]:
    fake_run = MagicMock()
    fake_run.id = run_id
    fake_run.status = PipelineRunStatus.pending
    fake_run.pipeline_name = 'access_apply_pipeline'
    fake_run.pipeline_version = 1

    orchestrator = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_run, created=True)
    return orchestrator, fake_run


# ===========================================================================
# Test 1 — DELEGATED initiative carry-over
# ===========================================================================


@pytest.mark.asyncio
async def test_delegated_initiative_carry_over(session_factory) -> None:  # type: ignore[no-untyped-def]
    """G6 delegated scenario:

    1. Seed Employee A (delegator) + Employee B (delegatee).
    2. Seed a delegated Initiative on B's subject_ref:
       type=delegated, origin='delegation:<A_subject_ref>'.
    3. POST /plans for B → carry-over picks up the delegated initiative.
    4. Plan item: initiatives list contains delegated entry with correct origin.
    5. execute_plan: items done; Initiative in DB has origin='delegation:<A_subject_ref>'.
    """
    # -------------------------------------------------------------------
    # Step 1: seed
    # -------------------------------------------------------------------
    async with session_factory() as session:
        org = await _seed_org_unit(session, f'eng-g6-del-{uuid.uuid4().hex[:4]}')
        _emp_a, subj_a = await _seed_employee_with_subject(session, org.id, 'delegator-A')
        _emp_b, subj_b = await _seed_employee_with_subject(session, org.id, 'delegatee-B')
        await _seed_connector(session, _DELEGATED_APP_ID)
        await session.commit()

    subject_ref_a = str(subj_a.id)
    subject_ref_b = str(subj_b.id)

    # -------------------------------------------------------------------
    # Step 2: seed delegated initiative for B
    #
    # Per _format_carry_over_origin:
    #   delegated → f'delegation:{initiative.origin}'
    # So the seeded initiative.origin must be subject_ref_a (the raw delegator
    # reference), and carry-over will produce origin='delegation:<subject_ref_a>'.
    # -------------------------------------------------------------------
    # origin stored on the Initiative row = delegator subject_ref (raw, no prefix)
    delegated_raw_origin = subject_ref_a

    async with session_factory() as session:
        delegated_initiative = Initiative(
            id=uuid.uuid4(),
            access_fact_id=uuid.uuid4(),  # UUID anchor, no PG FK
            type=InitiativeType.delegated,
            origin=delegated_raw_origin,
            subject_ref=subject_ref_b,
            subject_type='employee',
        )
        session.add(delegated_initiative)
        await session.commit()

    delegated_initiative_id = delegated_initiative.id

    # -------------------------------------------------------------------
    # Step 3: POST /plans for B — policy has no birthright, only carry-over
    #
    # The Initiative table does not store application/target_descriptor.
    # We patch _initiatives_to_current_initiatives to supply the necessary
    # application and target_descriptor so PDP carry-over can produce a
    # ProjectedFact (with a non-empty application).
    # -------------------------------------------------------------------
    def _delegated_initiatives_to_current(
        initiatives: list,
    ) -> list[CurrentInitiative]:
        result = []
        for init in initiatives:
            result.append(
                CurrentInitiative(
                    id=init.id,
                    access_fact_id=init.access_fact_id,
                    type=init.type,
                    origin=init.origin,
                    valid_from=getattr(init, 'valid_from', None),
                    valid_until=getattr(init, 'valid_until', None),
                    application=_DELEGATED_APP_ID,
                    target_descriptor={'fact_kind': 'role_grant', 'role_ref': 'delegated_role_g6'},
                )
            )
        return result

    run_id = uuid.uuid4()
    orchestrator, _ = _make_orchestrator_and_run(run_id)
    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    app = _make_app(session_factory, orchestrator=orchestrator, pipelines=pipelines, rule_pack=_DELEGATED_RULES)

    with patch(
        'src.engines.access_plan.service._initiatives_to_current_initiatives',
        side_effect=_delegated_initiatives_to_current,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            resp = await client.post('/api/v0/plans', json={'subject_ref': subject_ref_b})

    assert resp.status_code == 201, f'Plan creation failed: {resp.text}'
    plan_data = resp.json()
    plan_id = uuid.UUID(plan_data['id'])

    # -------------------------------------------------------------------
    # Step 4: Verify delegated initiative in plan items
    # -------------------------------------------------------------------
    items_data = plan_data.get('items', [])
    assert len(items_data) >= 1, f'Expected ≥1 plan items from delegated carry-over, got {len(items_data)}'

    # At least one item must have delegated initiative with correct origin
    delegated_items = [
        item for item in items_data if any(i.get('type') == 'delegated' for i in item.get('initiatives', []))
    ]
    assert len(delegated_items) >= 1, f'Expected ≥1 item with delegated initiative, got none. items={items_data}'
    delegated_item = delegated_items[0]
    delegated_initiatives_in_item = [i for i in delegated_item['initiatives'] if i.get('type') == 'delegated']
    assert len(delegated_initiatives_in_item) >= 1

    # origin produced by carry-over = 'delegation:<delegated_raw_origin>'
    # = 'delegation:<subject_ref_a>'
    actual_origin = delegated_initiatives_in_item[0].get('origin', '')
    expected_carry_over_origin = f'delegation:{delegated_raw_origin}'
    assert actual_origin == expected_carry_over_origin, (
        f'Delegated initiative origin must be {expected_carry_over_origin!r}, got: {actual_origin!r}'
    )

    # source_initiative_id must be set (carry-over from the seeded initiative)
    src_init_id = delegated_initiatives_in_item[0].get('source_initiative_id')
    assert src_init_id is not None, 'Delegated carry-over initiative must have source_initiative_id set'
    assert uuid.UUID(src_init_id) == delegated_initiative_id, (
        f'source_initiative_id must equal seeded initiative id {delegated_initiative_id}, got {src_init_id}'
    )

    # -------------------------------------------------------------------
    # Step 5: execute_plan
    # -------------------------------------------------------------------
    async with session_factory() as session:
        pipeline_run = PipelineRun(
            id=run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(plan_id)},
            content_hash='g6_del_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        resp_apply = await client.post(f'/api/v0/plans/{plan_id}/apply', json={})

    assert resp_apply.status_code == 201, f'Apply failed: {resp_apply.text}'

    async with session_factory() as session:
        db_items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_id))
        db_items = list(db_items_result.scalars().all())

    n_items = len(db_items)
    assert n_items >= 1

    stub_connector = _G6StubConnector(n_items=n_items)
    stub_sync = _G6StubSyncService()
    initiative_service = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        counts = await execute_plan(
            session,
            plan_id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=initiative_service,
        )

    assert counts.items_done == n_items, f'Expected {n_items} items done, got {counts.items_done}'
    assert counts.items_failed == 0, f'Expected 0 failed, got {counts.items_failed}'

    # All executions done
    async with session_factory() as session:
        exec_result = await session.execute(sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan_id))
        executions = list(exec_result.scalars().all())

    assert len(executions) == n_items
    for exc in executions:
        assert exc.status == PlanItemExecutionStatus.done, f'Item {exc.item_id} status={exc.status}'

    # Lease released
    async with session_factory() as session:
        lease_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref_b)
        )
        lease = lease_result.scalar_one_or_none()
    assert lease is None, 'AccessApplyActive lease must be released after execute_plan'

    # At least one Initiative in DB for B must have origin containing 'delegation:'
    async with session_factory() as session:
        init_result = await session.execute(
            sa.select(Initiative).where(
                Initiative.subject_ref == subject_ref_b,
                Initiative.type == InitiativeType.delegated,
            )
        )
        db_delegated = list(init_result.scalars().all())

    assert len(db_delegated) >= 1, 'At least one Initiative with type=delegated must exist for B after execute_plan'
    # The F3 chain creates a new Initiative with the carry-over origin
    # ('delegation:<subject_ref_a>'). The seeded raw initiative (origin=subject_ref_a,
    # without prefix) also exists — we check that at least one has 'delegation:' prefix.
    origins_with_prefix = [init for init in db_delegated if 'delegation:' in init.origin]
    assert len(origins_with_prefix) >= 1, (
        f'At least one delegated Initiative must have "delegation:" prefix in origin. '
        f'Found origins: {[i.origin for i in db_delegated]}'
    )


# ===========================================================================
# Test 2 — GRACE initiative carry-over
# ===========================================================================


@pytest.mark.asyncio
async def test_grace_initiative_carry_over(session_factory) -> None:  # type: ignore[no-untyped-def]
    """G6 grace scenario:

    1. Seed Employee C with a closed 'requested' Initiative (original).
    2. Seed a grace Initiative for C: type=grace,
       origin='grace:<original_requested_id>', valid_until=now()+7days.
    3. POST /plans for C → PDP carry-over picks up grace initiative (still valid).
    4. Plan item has grace initiative with correct origin (points back to original).
    5. execute_plan: items done; Initiative in DB with origin='grace:...'.
    6. Initiative chain verified: grace.origin → original_requested_id.
    """
    # -------------------------------------------------------------------
    # Step 1: seed Employee C + closed requested initiative
    # -------------------------------------------------------------------
    async with session_factory() as session:
        org = await _seed_org_unit(session, f'eng-g6-grace-{uuid.uuid4().hex[:4]}')
        _emp_c, subj_c = await _seed_employee_with_subject(session, org.id, 'grace-C')
        await _seed_connector(session, _GRACE_APP_ID)

        # Seed original 'requested' Initiative that was subsequently revoked
        # (valid_until set to the past)
        original_requested = Initiative(
            id=uuid.uuid4(),
            access_fact_id=uuid.uuid4(),
            type=InitiativeType.requested,
            origin='request:access-request-001',
            subject_ref=str(subj_c.id),
            subject_type='employee',
            valid_until=datetime.now(UTC) - timedelta(hours=1),  # already closed/revoked
        )
        session.add(original_requested)
        await session.flush()
        original_requested_id = original_requested.id

        # Seed grace Initiative: valid for 7 more days, origin chains to the
        # original requested initiative
        grace_origin = f'grace:{original_requested_id}'
        grace_valid_until = datetime.now(UTC) + timedelta(days=7)

        grace_initiative = Initiative(
            id=uuid.uuid4(),
            access_fact_id=uuid.uuid4(),
            type=InitiativeType.grace,
            origin=grace_origin,
            subject_ref=str(subj_c.id),
            subject_type='employee',
            valid_until=grace_valid_until,
        )
        session.add(grace_initiative)
        await session.commit()

    subject_ref_c = str(subj_c.id)
    grace_initiative_id = grace_initiative.id

    # -------------------------------------------------------------------
    # Step 3: POST /plans for C
    #
    # Patch _initiatives_to_current_initiatives to supply application and
    # target_descriptor for the grace initiative (not stored in Initiative table).
    # Only the non-expired grace initiative should appear in carry-over;
    # the original requested (expired) is filtered out by the fetch query.
    # -------------------------------------------------------------------
    def _grace_initiatives_to_current(
        initiatives: list,
    ) -> list[CurrentInitiative]:
        result = []
        for init in initiatives:
            result.append(
                CurrentInitiative(
                    id=init.id,
                    access_fact_id=init.access_fact_id,
                    type=init.type,
                    origin=init.origin,
                    valid_from=getattr(init, 'valid_from', None),
                    valid_until=getattr(init, 'valid_until', None),
                    application=_GRACE_APP_ID,
                    target_descriptor={'fact_kind': 'role_grant', 'role_ref': 'grace_role_g6'},
                )
            )
        return result

    run_id = uuid.uuid4()
    orchestrator, _ = _make_orchestrator_and_run(run_id)
    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    app = _make_app(session_factory, orchestrator=orchestrator, pipelines=pipelines, rule_pack=_GRACE_RULES)

    with patch(
        'src.engines.access_plan.service._initiatives_to_current_initiatives',
        side_effect=_grace_initiatives_to_current,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            resp = await client.post('/api/v0/plans', json={'subject_ref': subject_ref_c})

    assert resp.status_code == 201, f'Plan creation failed: {resp.text}'
    plan_data = resp.json()
    plan_id = uuid.UUID(plan_data['id'])

    # -------------------------------------------------------------------
    # Step 4: Verify grace initiative in plan items
    # -------------------------------------------------------------------
    items_data = plan_data.get('items', [])
    assert len(items_data) >= 1, f'Expected ≥1 plan items from grace carry-over, got {len(items_data)}'

    grace_items = [item for item in items_data if any(i.get('type') == 'grace' for i in item.get('initiatives', []))]
    assert len(grace_items) >= 1, f'Expected ≥1 item with grace initiative, got none. items={items_data}'

    grace_item = grace_items[0]
    grace_initiatives_in_item = [i for i in grace_item['initiatives'] if i.get('type') == 'grace']
    assert len(grace_initiatives_in_item) >= 1

    # Per _format_carry_over_origin: grace → f'grace:{initiative.id}'
    # So carry-over produces origin='grace:<grace_initiative_id>'.
    actual_grace_origin = grace_initiatives_in_item[0].get('origin', '')
    expected_carry_over_origin = f'grace:{grace_initiative_id}'
    assert actual_grace_origin == expected_carry_over_origin, (
        f'Grace initiative carry-over origin must be {expected_carry_over_origin!r}, got {actual_grace_origin!r}'
    )

    # source_initiative_id must be set (the seeded grace initiative)
    src_init_id = grace_initiatives_in_item[0].get('source_initiative_id')
    assert src_init_id is not None, 'Grace carry-over initiative must have source_initiative_id set'
    assert uuid.UUID(src_init_id) == grace_initiative_id, (
        f'source_initiative_id must equal grace initiative id {grace_initiative_id}, got {src_init_id}'
    )

    # -------------------------------------------------------------------
    # Step 5: execute_plan
    # -------------------------------------------------------------------
    async with session_factory() as session:
        pipeline_run = PipelineRun(
            id=run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(plan_id)},
            content_hash='g6_grace_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        resp_apply = await client.post(f'/api/v0/plans/{plan_id}/apply', json={})

    assert resp_apply.status_code == 201, f'Apply failed: {resp_apply.text}'

    async with session_factory() as session:
        db_items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_id))
        db_items = list(db_items_result.scalars().all())

    n_items = len(db_items)
    assert n_items >= 1

    stub_connector = _G6StubConnector(n_items=n_items)
    stub_sync = _G6StubSyncService()
    initiative_service = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        counts = await execute_plan(
            session,
            plan_id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=initiative_service,
        )

    assert counts.items_done == n_items, f'Expected {n_items} items done, got {counts.items_done}'
    assert counts.items_failed == 0, f'Expected 0 failed, got {counts.items_failed}'

    # All executions done
    async with session_factory() as session:
        exec_result = await session.execute(sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan_id))
        executions = list(exec_result.scalars().all())

    assert len(executions) == n_items
    for exc in executions:
        assert exc.status == PlanItemExecutionStatus.done, f'Item {exc.item_id} status={exc.status}'

    # Lease released
    async with session_factory() as session:
        lease_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref_c)
        )
        lease = lease_result.scalar_one_or_none()
    assert lease is None, 'AccessApplyActive lease must be released after execute_plan'

    # At least one Initiative in DB for C must have origin 'grace:...'
    async with session_factory() as session:
        grace_db_result = await session.execute(
            sa.select(Initiative).where(
                Initiative.subject_ref == subject_ref_c,
                Initiative.type == InitiativeType.grace,
            )
        )
        db_grace = list(grace_db_result.scalars().all())

    assert len(db_grace) >= 1, 'At least one Initiative with type=grace must exist for C after execute_plan'

    # -------------------------------------------------------------------
    # Step 6: Verify initiative chain
    # origin of F3-created grace initiative → points to original_requested_id
    # -------------------------------------------------------------------
    for db_g in db_grace:
        assert 'grace:' in db_g.origin, f'Grace Initiative origin must contain "grace:", got: {db_g.origin!r}'

    # The seeded grace Initiative (which carry-over was based on) must still exist
    # (audit trail, NO DELETE semantics)
    async with session_factory() as session:
        seeded_grace_result = await session.execute(sa.select(Initiative).where(Initiative.id == grace_initiative_id))
        seeded_grace_db = seeded_grace_result.scalar_one_or_none()

    assert seeded_grace_db is not None, 'Original seeded grace Initiative must still exist (audit trail)'

    # Verify origin chain: seeded grace Initiative → points to original_requested_id
    expected_chain_origin = f'grace:{original_requested_id}'
    assert seeded_grace_db.origin == expected_chain_origin, (
        f'Grace chain: origin must be {expected_chain_origin!r}, got {seeded_grace_db.origin!r}'
    )

    # The original requested Initiative must still exist and remain closed
    async with session_factory() as session:
        original_result = await session.execute(sa.select(Initiative).where(Initiative.id == original_requested_id))
        original_db = original_result.scalar_one_or_none()

    assert original_db is not None, 'Original requested Initiative must still exist (audit trail)'
    assert original_db.valid_until is not None, 'Original requested Initiative must remain closed (valid_until set)'
    assert original_db.valid_until < datetime.now(UTC), (
        'Original requested Initiative must remain in the past (revoked)'
    )
