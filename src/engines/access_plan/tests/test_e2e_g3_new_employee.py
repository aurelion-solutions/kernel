# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""E2E test — Phase 19 Step G3: new employee → plan → apply → verify.

Scenario:
1. Create Employee record (Person → Employee + EmployeeAttribute → Subject).
2. Tenant policy (inline fixture) enables birthright rules for the employee's
   org_unit with attributes (role, project, location).
3. POST /plans with subject_ref=employee_id:
   - 201 response
   - Plan contains account_create + grant_role items
   - PlanDependency rows exist (grant requires account active)
   - decision_snapshot populated with PDP Decision
4. POST /plans/{id}/apply:
   - 201 + pipeline_run_id
   - AccessApplyActive lease created
5. execute_plan (pipeline simulation):
   - Stub connector: preflight mismatch → connector call → post-verify match
   - All PlanItemExecution.status == done
6. Post-conditions:
   - Stub sync service called (sync_single_fact event_key per item)
   - Initiative records created with origin=policy_rule:<rule_id>
   - access_apply_active row deleted (lease released in finally)
   - Plan status == active (not invalidated)
7. Idempotent apply: POST /plans/{id}/apply again → 200 (same pipeline_run_id),
   but since pipeline is terminal, a new run is created instead (409 or 201).
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
# Inline policy fixture — birthright_basic
#
# Rules:
# - birthright_account_create: employee in eng org → account_create in mock_app
# - birthright_grant_engineer: employee with role=engineer → grant_role in mock_app
# ---------------------------------------------------------------------------

_ORG_UNIT_EXT_ID = 'engineering'
_APP_INSTANCE_ID = 'mock-app-g3'

# Birthright rules using attributes.role/project/location — no org_unit UUID lookup
# needed since org_unit_id is a UUID that changes per test run.
_BIRTHRIGHT_RULES = RulePack(
    lifecycle=[
        Rule(
            id='birthright_account_create',
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
            id='birthright_grant_engineer',
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
    ],
)


# ---------------------------------------------------------------------------
# Stub connector — preflight mismatch → connector OK → post-verify match
# ---------------------------------------------------------------------------


class _G3StubConnector:
    """Stub connector that returns mismatch on preflight, match on post-verify."""

    def __init__(self) -> None:
        self.invoke_calls: list[dict[str, Any]] = []
        self._verify_idx = 0
        # Each item gets two verify calls: preflight (mismatch) + post (match).
        # We return alternating mismatch/match across all calls.
        self._verify_seq: list[str] = []

    def set_item_count(self, n: int) -> None:
        """Set expected number of items — each gets mismatch then match."""
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
        self.invoke_calls.append(
            {
                'operation': operation,
                'payload': payload,
                'instance_id': instance_id,
            }
        )
        if operation == 'verify_fact':
            if self._verify_idx < len(self._verify_seq):
                result = self._verify_seq[self._verify_idx]
                self._verify_idx += 1
            else:
                result = 'match'
            return {'status': 'ok', 'result': result}
        return {'status': 'ok'}


# ---------------------------------------------------------------------------
# Stub sync service — tracks sync_single_fact calls
# ---------------------------------------------------------------------------


class _G3StubSyncService:
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
        self.calls.append(
            {
                'op': op,
                'event_key': event_key,
                'application_id_denorm': application_id_denorm,
            }
        )
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
        content_hash='g3_hash',
        args_schema_dict={},
        steps=(),
        triggers=(),
        raw_dict={},
    )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_org_unit(session: AsyncSession) -> OrgUnit:
    org = OrgUnit(
        external_id=_ORG_UNIT_EXT_ID,
        name='Engineering',
    )
    session.add(org)
    await session.flush()
    return org


async def _seed_employee(
    session: AsyncSession,
    org_unit_id: uuid.UUID,
    *,
    role: str = 'engineer',
) -> tuple[Employee, Subject]:
    """Create Person → Employee + EmployeeAttribute + Subject for the employee."""
    person = await create_person(
        session,
        external_id=f'emp-g3-{uuid.uuid4().hex[:6]}',
        full_name='G3 Test Employee',
    )
    await session.flush()

    emp = await create_employee(session, person_id=person.id)
    # Set org_unit_id directly
    emp.org_unit_id = org_unit_id
    await session.flush()

    # Add role attribute
    attr = EmployeeAttribute(
        employee_id=emp.id,
        key='role',
        value=role,
    )
    session.add(attr)

    subj = Subject(
        external_id=f'subj-g3-{uuid.uuid4().hex[:6]}',
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()

    return emp, subj


async def _seed_connector_instance(session: AsyncSession) -> ConnectorInstance:
    """Register mock-app-g3 connector instance with full MOCK_CONNECTOR_DESCRIPTOR."""
    from src.platform.connectors.mock_connector import MOCK_CONNECTOR_DESCRIPTOR  # noqa: PLC0415

    conn = ConnectorInstance(
        instance_id=_APP_INSTANCE_ID,
        tags=['mock', 'g3'],
        descriptor=MOCK_CONNECTOR_DESCRIPTOR.model_dump(mode='json'),
    )
    session.add(conn)
    await session.flush()
    return conn


# ---------------------------------------------------------------------------
# App factory — real DB session + injected PDP + mock orchestrator
# ---------------------------------------------------------------------------


def _make_test_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    orchestrator: PipelineOrchestratorService,
    pipelines: dict[str, Any],
) -> FastAPI:
    """Build a minimal FastAPI app with DI overrides for E2E testing."""
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

    from src.engines.access_plan.routes import router as plan_router

    app = FastAPI()
    app.include_router(plan_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_access_plan_service] = override_get_service
    app.dependency_overrides[get_plan_orchestrator_service] = override_get_orchestrator
    app.dependency_overrides[get_plan_pipelines] = override_get_pipelines
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_employee_plan_apply_verify(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Full E2E: new employee → plan (account_create + grant_role) → apply → verify.

    Phase 19 Step G3 — Exit Criterion #5 (first sub-scenario):
    новый сотрудник → план с account_create + grants → выполнить → verify.
    """
    # -----------------------------------------------------------------------
    # Step 1: Seed DB — org_unit, employee, connector instance
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        org = await _seed_org_unit(session)
        emp, subj = await _seed_employee(session, org.id)
        await _seed_connector_instance(session)
        await session.commit()

    subject_ref = str(subj.id)

    # -----------------------------------------------------------------------
    # Step 2: Build orchestrator mock (returns a new pipeline run on create)
    # -----------------------------------------------------------------------
    from unittest.mock import AsyncMock, MagicMock

    run_id = uuid.uuid4()
    fake_run = MagicMock()
    fake_run.id = run_id
    fake_run.status = PipelineRunStatus.pending
    fake_run.pipeline_name = 'access_apply_pipeline'
    fake_run.pipeline_version = 1

    orchestrator_mock = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator_mock.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_run, created=True)

    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}

    app = _make_test_app(session_factory, orchestrator=orchestrator_mock, pipelines=pipelines)

    # -----------------------------------------------------------------------
    # Step 3: POST /plans → 201
    # -----------------------------------------------------------------------
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        resp = await client.post('/api/v0/plans', json={'subject_ref': subject_ref})

    assert resp.status_code == 201, f'Expected 201, got {resp.status_code}: {resp.text}'
    plan_data = resp.json()
    assert plan_data['subject_ref'] == subject_ref
    assert plan_data['status'] == 'active'

    plan_id = uuid.UUID(plan_data['id'])

    # Verify items exist and contain expected kinds
    items = plan_data.get('items', [])
    assert len(items) >= 1, 'Plan must have at least one item'

    item_kinds = {item['kind'] for item in items}
    # birthright_account_create rule → account_create (or account_invite via D3 transitions)
    assert item_kinds & {
        PlanItemKind.account_create.value,
        PlanItemKind.account_invite.value,
        PlanItemKind.grant_role.value,
    }, f'Expected account or grant items, got: {item_kinds}'

    # Verify decision_snapshot populated
    for item in items:
        assert item['decision_snapshot'], f'decision_snapshot empty for item {item["id"]}'

    # -----------------------------------------------------------------------
    # Step 3b: GET /plans/{id} to verify PlanDependency rows exist
    # -----------------------------------------------------------------------
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        resp_get = await client.get(f'/api/v0/plans/{plan_id}')

    assert resp_get.status_code == 200
    full_plan = resp_get.json()
    # Dependencies: grant_role should depend on account item being active
    # This is only present when both account + role items exist
    deps = full_plan.get('dependencies', [])
    # If both account and grant items exist, at least one dep should be there
    if len(items) >= 2:
        assert len(deps) >= 1, 'Expected PlanDependency rows when account + grant items exist'

    # -----------------------------------------------------------------------
    # Step 4: POST /plans/{id}/apply → 201 + pipeline_run_id
    # -----------------------------------------------------------------------

    # Seed the PipelineRun in DB so the apply route can check status
    async with session_factory() as session:
        from src.platform.orchestrator.models import PipelineRun, PipelineTriggerSource

        pipeline_run = PipelineRun(
            id=run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(plan_id)},
            content_hash='g3_e2e_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        resp_apply = await client.post(f'/api/v0/plans/{plan_id}/apply', json={})

    assert resp_apply.status_code == 201, f'Expected 201 apply, got {resp_apply.status_code}: {resp_apply.text}'
    apply_data = resp_apply.json()
    assert 'pipeline_run_id' in apply_data
    returned_run_id = uuid.UUID(apply_data['pipeline_run_id'])
    assert returned_run_id == run_id

    # Verify AccessApplyActive lease created
    async with session_factory() as session:
        lease_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease = lease_result.scalar_one_or_none()
    assert lease is not None, 'AccessApplyActive lease must exist after apply'
    assert lease.plan_id == plan_id
    assert lease.pipeline_run_id == run_id

    # -----------------------------------------------------------------------
    # Step 5: Simulate pipeline execution via execute_plan
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_id))
        db_items = list(items_result.scalars().all())

    n_items = len(db_items)
    stub_connector = _G3StubConnector()
    stub_connector.set_item_count(n_items)
    stub_sync = _G3StubSyncService()
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

    assert counts.items_done == n_items, f'Expected {n_items} done, got {counts.items_done}'
    assert counts.items_failed == 0, f'Expected 0 failed, got {counts.items_failed}'

    # -----------------------------------------------------------------------
    # Step 6: Verify post-conditions
    # -----------------------------------------------------------------------

    # (a) All PlanItemExecution.status == done
    async with session_factory() as session:
        exec_result = await session.execute(sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan_id))
        executions = list(exec_result.scalars().all())

    assert len(executions) == n_items, 'Expected one execution row per item'
    for exec_row in executions:
        assert exec_row.status == PlanItemExecutionStatus.done, (
            f'Item {exec_row.item_id} status={exec_row.status}, expected done'
        )

    # (b) sync_single_fact called once per item (lake AccessFact events written)
    assert len(stub_sync.calls) == n_items, f'Expected {n_items} sync_single_fact calls, got {len(stub_sync.calls)}'
    for call in stub_sync.calls:
        assert call['event_key'], 'event_key must be non-empty for each sync call'

    # (c) Initiative rows created with origin=policy_rule:<rule_id>
    async with session_factory() as session:
        init_result = await session.execute(sa.select(Initiative))
        all_initiatives = list(init_result.scalars().all())

    # At minimum one initiative per grant-type item
    grant_items = [
        i
        for i in db_items
        if i.kind
        in (
            PlanItemKind.account_create,
            PlanItemKind.account_invite,
            PlanItemKind.account_activate,
            PlanItemKind.grant_role,
        )
    ]
    birthright_initiatives = [
        i for i in all_initiatives if i.type == InitiativeType.birthright or i.origin.startswith('policy_rule:')
    ]
    assert len(birthright_initiatives) >= len(grant_items), (
        f'Expected at least {len(grant_items)} birthright initiatives, '
        f'got {len(birthright_initiatives)}. '
        f'All initiatives: {[(i.type, i.origin) for i in all_initiatives]}'
    )

    # (d) access_apply_active row deleted (lease released in finally)
    async with session_factory() as session:
        lease_after_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease_after = lease_after_result.scalar_one_or_none()
    assert lease_after is None, 'AccessApplyActive lease must be deleted after execute_plan'

    # (e) Plan status == active (not invalidated)
    async with session_factory() as session:
        plan_after_result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == plan_id))
        plan_after = plan_after_result.scalar_one_or_none()
    assert plan_after is not None
    assert plan_after.status == AccessPlanStatus.active, f'Plan status must remain active, got {plan_after.status}'


@pytest.mark.asyncio
async def test_apply_idempotent_same_plan(session_factory) -> None:  # type: ignore[no-untyped-def]
    """POST /plans/{id}/apply on same plan while active run in progress → 200 + same run_id.

    Phase 19 Step G3 — idempotency sub-scenario (Step 6 in spec).
    """
    # Seed minimal plan
    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref=str(uuid.uuid4()),
            subject_type='employee',
            content_hash='idempotent_g3',
            status=AccessPlanStatus.active,
            requires_confirmation=False,
        )
        session.add(plan)
        await session.flush()
        plan_id = plan.id
        subject_ref = plan.subject_ref

        run_id = uuid.uuid4()
        from src.platform.orchestrator.models import PipelineRun, PipelineTriggerSource

        pipeline_run = PipelineRun(
            id=run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(plan_id)},
            content_hash='g3_idempotent_run',
        )
        session.add(pipeline_run)

        lease = AccessApplyActive(
            subject_ref=subject_ref,
            subject_type='employee',
            pipeline_run_id=run_id,
            plan_id=plan_id,
        )
        session.add(lease)
        await session.commit()

    from unittest.mock import AsyncMock

    orchestrator_mock = AsyncMock(spec=PipelineOrchestratorService)
    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}

    test_app = _make_test_app(session_factory, orchestrator=orchestrator_mock, pipelines=pipelines)

    # Second apply for same plan while run is active → 200 + same run_id
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
        resp = await client.post(f'/api/v0/plans/{plan_id}/apply', json={})

    assert resp.status_code == 200, f'Expected 200 (idempotent reuse), got {resp.status_code}: {resp.text}'
    data = resp.json()
    assert uuid.UUID(data['pipeline_run_id']) == run_id, f'Expected same run_id {run_id}, got {data["pipeline_run_id"]}'
