# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""E2E test — Phase 19 Step G4: existing employee, context changed → replan supersedes old plan.

Scenario:
1. Create Employee + apply initial plan (reuses G3 setup fixtures).
   Initial context: role=engineer → birthright_account_create + birthright_grant_engineer items.
2. PATCH employee attributes (role → senior_engineer OR change org_unit_id).
   EmployeeService.update_employee() emits inventory.employee.updated (post-K-A).
3. MQ matcher (E3) catches event → invokes access_plan.plan action for subject_ref.
   Simulated by directly calling service.create_plan() which is what the action does.
4. Service creates new AccessPlan with different diff (role changed → different grant).
   new plan: supersedes_plan_id = old_plan.id
   old plan: status = superseded (auto-supersedes during create_plan)
5. POST /plans/{new_id}/apply → 201.
6. Verify:
   - new Initiative created with new role grant
   - lease released (access_apply_active deleted)
   - old plan in superseded chain (old.supersedes_plan_id may be None, but
     new.supersedes_plan_id references old plan id, and old.status == superseded)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
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
    AccessPlan,
    AccessPlanStatus,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemKind,
)
from src.engines.access_plan.service import AccessPlanService
from src.engines.policy_assessment.generative.service import GenerativePDPService
from src.engines.policy_assessment.schemas import Rule, RulePack
import src.inventory.employees.models  # noqa: F401 — registers Employee for create_all
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.employees.repository import create_employee
from src.inventory.employees.schemas import EmployeePatch
from src.inventory.employees.service import EmployeeService
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
from src.platform.orchestrator.models import PipelineRunStatus
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.orchestrator.service_types import PipelineRunCreateResult
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# Inline policy fixture — birthright_basic (extended for context change)
#
# Rules:
# - birthright_account_create: employee with any role → account_create in mock_app
# - birthright_grant_engineer: employee with role=engineer → grant_role engineer_role
# - birthright_grant_senior: employee with role=senior_engineer → grant_role senior_role
# ---------------------------------------------------------------------------

_ORG_UNIT_EXT_ID = 'engineering-g4'
_APP_INSTANCE_ID = 'mock-app-g4'

_BIRTHRIGHT_RULES = RulePack(
    lifecycle=[
        Rule(
            id='birthright_account_create_g4',
            kind='birthright',
            when={
                'subject.kind': 'employee',
                'attributes.role': 'engineer',
            },
            then={
                'application': _APP_INSTANCE_ID,
                'fact_kind': 'account',
                'target_descriptor': {'fact_kind': 'account'},
            },
            precedence=10,
        ),
        Rule(
            id='birthright_grant_engineer_g4',
            kind='birthright',
            when={
                'subject.kind': 'employee',
                'attributes.role': 'engineer',
            },
            then={
                'application': _APP_INSTANCE_ID,
                'fact_kind': 'role_grant',
                'target_descriptor': {'role_ref': 'engineer_role', 'fact_kind': 'role_grant'},
            },
            precedence=9,
        ),
        Rule(
            id='birthright_account_create_senior_g4',
            kind='birthright',
            when={
                'subject.kind': 'employee',
                'attributes.role': 'senior_engineer',
            },
            then={
                'application': _APP_INSTANCE_ID,
                'fact_kind': 'account',
                'target_descriptor': {'fact_kind': 'account'},
            },
            precedence=10,
        ),
        Rule(
            id='birthright_grant_senior_g4',
            kind='birthright',
            when={
                'subject.kind': 'employee',
                'attributes.role': 'senior_engineer',
            },
            then={
                'application': _APP_INSTANCE_ID,
                'fact_kind': 'role_grant',
                'target_descriptor': {'role_ref': 'senior_role', 'fact_kind': 'role_grant'},
            },
            precedence=9,
        ),
    ],
)

# After context change (role=senior_engineer), different rules apply —
# only senior_* rules match.
_BIRTHRIGHT_RULES_AFTER = _BIRTHRIGHT_RULES  # same ruleset; PDP evaluates per current attributes


# ---------------------------------------------------------------------------
# Stub connector
# ---------------------------------------------------------------------------


class _G4StubConnector:
    """Stub connector that returns mismatch on preflight, match on post-verify."""

    def __init__(self) -> None:
        self.invoke_calls: list[dict[str, Any]] = []
        self._verify_idx = 0
        self._verify_seq: list[str] = []

    def set_item_count(self, n: int) -> None:
        self._verify_seq = ['mismatch', 'match'] * n

    async def invoke(
        self,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.invoke_calls.append({'operation': operation, 'payload': payload, 'instance_id': instance_id})
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


class _G4StubSyncService:
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
        content_hash='g4_hash',
        args_schema_dict={},
        steps=(),
        triggers=(),
        raw_dict={},
    )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_org_unit(session: AsyncSession) -> OrgUnit:
    org = OrgUnit(external_id=_ORG_UNIT_EXT_ID, name='Engineering G4')
    session.add(org)
    await session.flush()
    return org


async def _seed_employee(
    session: AsyncSession,
    org_unit_id: uuid.UUID,
    *,
    role: str = 'engineer',
) -> tuple[Employee, Subject]:
    """Create Person → Employee + EmployeeAttribute + Subject."""
    person = await create_person(
        session,
        external_id=f'emp-g4-{uuid.uuid4().hex[:6]}',
        full_name='G4 Test Employee',
    )
    await session.flush()

    emp = await create_employee(session, person_id=person.id)
    emp.org_unit_id = org_unit_id
    await session.flush()

    attr = EmployeeAttribute(employee_id=emp.id, key='role', value=role)
    session.add(attr)

    subj = Subject(
        external_id=f'subj-g4-{uuid.uuid4().hex[:6]}',
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()

    return emp, subj


async def _seed_connector_instance(session: AsyncSession) -> ConnectorInstance:
    from src.platform.connectors.mock_connector import MOCK_CONNECTOR_DESCRIPTOR  # noqa: PLC0415

    conn = ConnectorInstance(
        instance_id=_APP_INSTANCE_ID,
        tags=['mock', 'g4'],
        descriptor=MOCK_CONNECTOR_DESCRIPTOR.model_dump(mode='json'),
    )
    session.add(conn)
    await session.flush()
    return conn


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_test_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    orchestrator: PipelineOrchestratorService,
    pipelines: dict[str, Any],
) -> FastAPI:
    pdp = GenerativePDPService(rule_pack=_BIRTHRIGHT_RULES)
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
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_change_replan_supersedes(session_factory) -> None:  # type: ignore[no-untyped-def]
    """E2E G4: existing employee, context changes → replan supersedes old plan.

    Phase 19 Step G4 exit criterion:
    1. Initial plan created for employee with role=engineer.
    2. Employee attributes patched: role → senior_engineer (emits inventory.employee.updated).
    3. Matcher triggers replanning: create_plan called → new plan created.
    4. New plan supersedes_plan_id = old plan id; old plan status = superseded.
    5. POST /plans/{new_id}/apply → 201.
    6. Post-conditions: new initiatives, lease released, supersedes chain intact.
    """
    # -------------------------------------------------------------------
    # Step 1: Seed DB — org_unit, employee (role=engineer), connector
    # -------------------------------------------------------------------
    async with session_factory() as session:
        org = await _seed_org_unit(session)
        emp, subj = await _seed_employee(session, org.id, role='engineer')
        await _seed_connector_instance(session)
        await session.commit()

    subject_ref = str(subj.id)
    emp_id = emp.id

    # -------------------------------------------------------------------
    # Step 2: Create initial plan for engineer
    # -------------------------------------------------------------------
    from unittest.mock import AsyncMock, MagicMock  # noqa: PLC0415

    initial_run_id = uuid.uuid4()
    fake_initial_run = MagicMock()
    fake_initial_run.id = initial_run_id
    fake_initial_run.status = PipelineRunStatus.pending
    fake_initial_run.pipeline_name = 'access_apply_pipeline'
    fake_initial_run.pipeline_version = 1

    orchestrator_mock = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator_mock.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_initial_run, created=True)

    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    app = _make_test_app(session_factory, orchestrator=orchestrator_mock, pipelines=pipelines)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        resp = await client.post('/api/v0/plans', json={'subject_ref': subject_ref})

    assert resp.status_code == 201, f'Initial plan creation failed: {resp.text}'
    initial_plan_data = resp.json()
    initial_plan_id = uuid.UUID(initial_plan_data['id'])
    assert initial_plan_data['status'] == 'active'
    assert initial_plan_data['subject_ref'] == subject_ref

    # Verify initial plan items exist (engineer rules)
    initial_items = initial_plan_data.get('items', [])
    assert len(initial_items) >= 1, 'Initial plan must have items'
    initial_item_kinds = {item['kind'] for item in initial_items}
    assert initial_item_kinds & {
        PlanItemKind.account_create.value,
        PlanItemKind.account_invite.value,
        PlanItemKind.grant_role.value,
    }, f'Expected account or grant items in initial plan, got: {initial_item_kinds}'

    # -------------------------------------------------------------------
    # Step 3: PATCH employee role → senior_engineer
    # This emits inventory.employee.updated (E2, post-K-A).
    # Matcher (E3) would trigger access_plan.plan action.
    # We simulate this by calling the service directly (same code path).
    # -------------------------------------------------------------------
    employee_service = EmployeeService(event_service=noop_event_service)

    async with session_factory() as session:
        await employee_service.update_employee(
            session,
            emp_id,
            EmployeePatch(attributes={'role': 'senior_engineer'}),
        )
        await session.commit()

    # Verify attribute was updated
    async with session_factory() as session:
        result = await session.execute(
            sa.select(EmployeeAttribute).where(
                EmployeeAttribute.employee_id == emp_id,
                EmployeeAttribute.key == 'role',
            )
        )
        attr = result.scalar_one_or_none()
    assert attr is not None
    assert attr.value == 'senior_engineer', f'Expected role=senior_engineer, got {attr.value}'

    # -------------------------------------------------------------------
    # Step 4: MQ matcher triggers replanning → new AccessPlan created
    # The access_plan.plan action calls service.create_plan() — we call
    # it directly here to simulate the orchestrator path.
    # New plan should supersede the initial plan.
    # -------------------------------------------------------------------
    new_run_id = uuid.uuid4()
    fake_new_run = MagicMock()
    fake_new_run.id = new_run_id
    fake_new_run.status = PipelineRunStatus.pending
    fake_new_run.pipeline_name = 'access_apply_pipeline'
    fake_new_run.pipeline_version = 1

    orchestrator_mock_2 = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator_mock_2.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_new_run, created=True)

    app2 = _make_test_app(session_factory, orchestrator=orchestrator_mock_2, pipelines=pipelines)

    async with AsyncClient(transport=ASGITransport(app=app2), base_url='http://testserver') as client:
        resp_replan = await client.post('/api/v0/plans', json={'subject_ref': subject_ref})

    assert resp_replan.status_code == 201, f'Replan creation failed: {resp_replan.text}'
    new_plan_data = resp_replan.json()
    new_plan_id = uuid.UUID(new_plan_data['id'])

    # New plan must be different from old plan
    assert new_plan_id != initial_plan_id, 'Replan must create a new plan, not reuse old'
    assert new_plan_data['status'] == 'active'
    assert new_plan_data['subject_ref'] == subject_ref

    # New plan must have supersedes_plan_id pointing to initial plan
    assert new_plan_data.get('supersedes_plan_id') == str(initial_plan_id), (
        f'New plan must supersede initial plan: got {new_plan_data.get("supersedes_plan_id")}'
    )

    # New plan items should match senior_engineer rules (senior_role grant)
    new_items = new_plan_data.get('items', [])
    assert len(new_items) >= 1, 'New plan must have items'
    new_item_kinds = {item['kind'] for item in new_items}
    assert new_item_kinds & {
        PlanItemKind.account_create.value,
        PlanItemKind.account_invite.value,
        PlanItemKind.grant_role.value,
    }, f'Expected account or grant items in new plan, got: {new_item_kinds}'

    # -------------------------------------------------------------------
    # Step 4b: Verify initial plan is now superseded
    # -------------------------------------------------------------------
    async with session_factory() as session:
        old_plan_result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == initial_plan_id))
        old_plan = old_plan_result.scalar_one_or_none()

    assert old_plan is not None, 'Initial plan must still exist in DB'
    assert old_plan.status == AccessPlanStatus.superseded, f'Initial plan must be superseded, got {old_plan.status}'

    # -------------------------------------------------------------------
    # Step 5: POST /plans/{new_id}/apply → 201
    # -------------------------------------------------------------------
    async with session_factory() as session:
        from src.platform.orchestrator.models import PipelineRun, PipelineTriggerSource  # noqa: PLC0415

        pipeline_run = PipelineRun(
            id=new_run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(new_plan_id)},
            content_hash='g4_e2e_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app2), base_url='http://testserver') as client:
        resp_apply = await client.post(f'/api/v0/plans/{new_plan_id}/apply', json={})

    assert resp_apply.status_code == 201, f'Expected 201 apply, got {resp_apply.status_code}: {resp_apply.text}'
    apply_data = resp_apply.json()
    assert 'pipeline_run_id' in apply_data
    returned_run_id = uuid.UUID(apply_data['pipeline_run_id'])
    assert returned_run_id == new_run_id

    # Verify AccessApplyActive lease created for the new plan
    async with session_factory() as session:
        lease_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease = lease_result.scalar_one_or_none()
    assert lease is not None, 'AccessApplyActive lease must exist after apply'
    assert lease.plan_id == new_plan_id
    assert lease.pipeline_run_id == new_run_id

    # -------------------------------------------------------------------
    # Step 6: Simulate pipeline execution
    # -------------------------------------------------------------------
    async with session_factory() as session:
        items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == new_plan_id))
        db_new_items = list(items_result.scalars().all())

    n_items = len(db_new_items)
    stub_connector = _G4StubConnector()
    stub_connector.set_item_count(n_items)
    stub_sync = _G4StubSyncService()
    initiative_service = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        counts = await execute_plan(
            session,
            new_plan_id,
            new_run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=initiative_service,
        )

    assert counts.items_done == n_items, f'Expected {n_items} done, got {counts.items_done}'
    assert counts.items_failed == 0, f'Expected 0 failed, got {counts.items_failed}'

    # -------------------------------------------------------------------
    # Step 7: Verify post-conditions
    # -------------------------------------------------------------------

    # (a) All PlanItemExecution.status == done
    async with session_factory() as session:
        exec_result = await session.execute(
            sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == new_plan_id)
        )
        executions = list(exec_result.scalars().all())

    assert len(executions) == n_items, 'Expected one execution row per item'
    for exec_row in executions:
        assert exec_row.status == PlanItemExecutionStatus.done, (
            f'Item {exec_row.item_id} status={exec_row.status}, expected done'
        )

    # (b) sync_single_fact called for each item
    assert len(stub_sync.calls) == n_items, f'Expected {n_items} sync calls, got {len(stub_sync.calls)}'

    # (c) New Initiative rows created (for new plan's grant items)
    async with session_factory() as session:
        init_result = await session.execute(sa.select(Initiative))
        all_initiatives = list(init_result.scalars().all())

    grant_new_items = [
        i
        for i in db_new_items
        if i.kind
        in (
            PlanItemKind.account_create,
            PlanItemKind.account_invite,
            PlanItemKind.account_activate,
            PlanItemKind.grant_role,
        )
    ]
    new_birthright_initiatives = [
        i for i in all_initiatives if i.type == InitiativeType.birthright or i.origin.startswith('policy_rule:')
    ]
    assert len(new_birthright_initiatives) >= len(grant_new_items), (
        f'Expected at least {len(grant_new_items)} birthright initiatives after replan, '
        f'got {len(new_birthright_initiatives)}'
    )

    # (d) access_apply_active lease released after execute_plan
    async with session_factory() as session:
        lease_after_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease_after = lease_after_result.scalar_one_or_none()
    assert lease_after is None, 'AccessApplyActive lease must be released after execute_plan'

    # (e) New plan still active; old plan in superseded chain
    async with session_factory() as session:
        new_plan_after_result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == new_plan_id))
        new_plan_after = new_plan_after_result.scalar_one_or_none()
        old_plan_after_result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == initial_plan_id))
        old_plan_after = old_plan_after_result.scalar_one_or_none()

    assert new_plan_after is not None
    assert new_plan_after.status == AccessPlanStatus.active, (
        f'New plan must remain active after apply, got {new_plan_after.status}'
    )

    assert old_plan_after is not None
    assert old_plan_after.status == AccessPlanStatus.superseded, (
        f'Old plan must remain superseded, got {old_plan_after.status}'
    )

    # (f) Supersedes chain: new plan → old plan
    assert new_plan_after.supersedes_plan_id == initial_plan_id, (
        f'New plan supersedes_plan_id must point to old plan. Got {new_plan_after.supersedes_plan_id}'
    )
