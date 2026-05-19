# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""E2E test — Phase 19 Step G5: terminated employee → revoke plan → apply → verify.

Scenario:
1. Create Employee + seed fake effective grants (simulating post-initial-plan state).
2. PATCH employee.attributes['employment_status'] = 'terminated' (tenant-specific value).
   EmployeeService emits inventory.employee.updated (E2 after K-A refactor).
3. MQ matcher (E3) triggers access_plan.plan for subject.
   Simulated by calling service.create_plan() which is what the action does.
4. Policy fixture: terminated → no birthright rules match → PDP.generative returns
   empty desired state → diff = revoke all current facts.
5. Safe revoke threshold: revoke > 50% of current facts → requires_confirmation=True.
6. POST /plans/{id}/apply without confirm_destructive=true → 422
   (destructive_threshold_exceeded).
7. POST /plans/{id}/apply with confirm_destructive=true → 201.
8. execute_plan: revoke items execute.
   For each revoke: inventory_sync.sync_single_fact(op=revoke) + Initiative.close().
9. Verify:
   - All PlanItemExecution.status == done.
   - sync_single_fact called with op=revoke for each revoke item.
   - Initiatives closed (valid_until set, NO DELETE — audit trail preserved).
   - access_apply_active lease released.
   - Plan remains active; initial plan superseded (audit trail in DB).
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from src.engines.access_effective.models import EffectiveGrant, EffectiveGrantEffect
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
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus, PipelineTriggerSource
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.orchestrator.service_types import PipelineRunCreateResult
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# Inline policy fixture — G5: terminated → empty desired state
#
# Rules apply ONLY when employment_status='active'.
# When employment_status='terminated', no rule matches → empty desired state
# → diff = revoke all current effective grants.
#
# Carry-over is also blocked: _employment_blocks_carry_over returns True
# for 'terminated' (see GenerativePDPService._BLOCKING_EMPLOYMENT_STATUSES).
# ---------------------------------------------------------------------------

_ORG_UNIT_EXT_ID = 'engineering-g5'
_APP_INSTANCE_ID = 'mock-app-g5'

_G5_RULES = RulePack(
    lifecycle=[
        Rule(
            id='birthright_account_create_g5',
            kind='birthright',
            when={
                'subject.kind': 'employee',
                'attributes.employment_status': 'active',
            },
            then={
                'application': _APP_INSTANCE_ID,
                'fact_kind': 'account',
                'target_descriptor': {'fact_kind': 'account'},
            },
            precedence=10,
        ),
        Rule(
            id='birthright_grant_engineer_g5',
            kind='birthright',
            when={
                'subject.kind': 'employee',
                'attributes.employment_status': 'active',
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
# Stub connector
# ---------------------------------------------------------------------------


class _G5StubConnector:
    """Stub: preflight mismatch → connector call → post-verify match."""

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


class _G5StubSyncService:
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
        content_hash='g5_hash',
        args_schema_dict={},
        steps=(),
        triggers=(),
        raw_dict={},
    )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_org_unit(session: AsyncSession) -> OrgUnit:
    org = OrgUnit(external_id=_ORG_UNIT_EXT_ID, name='Engineering G5')
    session.add(org)
    await session.flush()
    return org


async def _seed_employee(
    session: AsyncSession,
    org_unit_id: uuid.UUID,
    *,
    employment_status: str = 'active',
) -> tuple[Employee, Subject]:
    """Create Person → Employee + EmployeeAttribute + Subject."""
    person = await create_person(
        session,
        external_id=f'emp-g5-{uuid.uuid4().hex[:6]}',
        full_name='G5 Test Employee',
    )
    await session.flush()

    emp = await create_employee(session, person_id=person.id)
    emp.org_unit_id = org_unit_id
    await session.flush()

    attr = EmployeeAttribute(employee_id=emp.id, key='employment_status', value=employment_status)
    session.add(attr)

    subj = Subject(
        external_id=f'subj-g5-{uuid.uuid4().hex[:6]}',
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
        tags=['mock', 'g5'],
        descriptor=MOCK_CONNECTOR_DESCRIPTOR.model_dump(mode='json'),
    )
    session.add(conn)
    await session.flush()
    return conn


async def _seed_initiatives_for_grants(
    session: AsyncSession,
    subject_ref: str,
    *,
    count: int = 2,
) -> list[Initiative]:
    """Seed Initiative rows directly to simulate grants created by initial apply.

    Uses subject_ref column directly (Phase 19 denormalized column).
    Each initiative gets a unique access_fact_id (UUID anchor, no PG FK).
    """
    initiatives = []
    for i in range(count):
        init = Initiative(
            id=uuid.uuid4(),
            access_fact_id=uuid.uuid4(),  # UUID anchor to lake event (no PG FK)
            type=InitiativeType.birthright,
            origin=f'policy_rule:birthright_rule_g5_{i}',
            subject_ref=subject_ref,
            subject_type='employee',
        )
        session.add(init)
        initiatives.append(init)
    await session.flush()
    return initiatives


# ---------------------------------------------------------------------------
# Fake effective grants for mock patching
# ---------------------------------------------------------------------------


def _make_fake_effective_grant(subject_id: uuid.UUID, initiative: Initiative) -> EffectiveGrant:
    """Build a fake EffectiveGrant (not persisted to DB) for mock patching."""
    from src.inventory.enums import Action  # noqa: PLC0415

    return EffectiveGrant(
        id=uuid.uuid4(),
        subject_id=subject_id,
        subject_kind=SubjectKind.employee,
        application_id=uuid.uuid4(),
        resource_id=uuid.uuid4(),
        action=Action.read,
        effect=EffectiveGrantEffect.allow,
        initiative_type=InitiativeType.birthright,
        initiative_origin=initiative.origin,
        valid_from=datetime.now(UTC),
        source_access_fact_id=initiative.access_fact_id,
        source_initiative_id=initiative.id,
    )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_test_app(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    orchestrator: PipelineOrchestratorService,
    pipelines: dict[str, Any],
    rule_pack: RulePack | None = None,
) -> FastAPI:
    pdp = GenerativePDPService(rule_pack=rule_pack or _G5_RULES)
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminated_employee_revoke_plan(session_factory) -> None:  # type: ignore[no-untyped-def]
    """E2E G5: terminated employee → revoke plan with requires_confirmation → apply → verify.

    Exit criteria (per spec):
    1. Birthright rules only match employment_status='active'.
    2. PATCH employment_status='terminated' → replan with empty desired.
    3. Diff: existing effective grants → revoke items.
    4. requires_confirmation=True (revoke > 50% of current grants).
    5. POST /apply without confirm_destructive → 422.
    6. POST /apply with confirm_destructive=true → 201.
    7. execute_plan: all revoke items done.
    8. sync_single_fact called with op=revoke per revoke item.
    9. Initiatives closed (valid_until set) — NO DELETE.
    10. Lease released; audit trail preserved.
    """
    # -------------------------------------------------------------------
    # Step 1: Seed DB — org_unit, employee (active), connector, initiatives
    # -------------------------------------------------------------------
    async with session_factory() as session:
        org = await _seed_org_unit(session)
        emp, subj = await _seed_employee(session, org.id, employment_status='active')
        await _seed_connector_instance(session)
        # Seed 2 initiatives to simulate existing grants (created by a prior apply).
        # These will be the revoke targets when employee is terminated.
        initiatives = await _seed_initiatives_for_grants(session, str(subj.id), count=2)
        await session.commit()

    subject_ref = str(subj.id)
    subject_uuid = subj.id
    emp_id = emp.id
    initiative_ids = [i.id for i in initiatives]

    # Build fake effective grants that correspond to the seeded initiatives.
    # These are NOT in the PG effective_grants table (partitioned, complex FK setup)
    # but are returned by a patched fetch_current_effective_grants.
    fake_grants = [_make_fake_effective_grant(subject_uuid, init) for init in initiatives]

    # -------------------------------------------------------------------
    # Step 2: PATCH employment_status → 'terminated'
    # -------------------------------------------------------------------
    employee_service = EmployeeService(event_service=noop_event_service)

    async with session_factory() as session:
        await employee_service.update_employee(
            session,
            emp_id,
            EmployeePatch(attributes={'employment_status': 'terminated'}),
        )
        await session.commit()

    # Verify attribute updated
    async with session_factory() as session:
        attr_result = await session.execute(
            sa.select(EmployeeAttribute).where(
                EmployeeAttribute.employee_id == emp_id,
                EmployeeAttribute.key == 'employment_status',
            )
        )
        attr = attr_result.scalar_one_or_none()
    assert attr is not None
    assert attr.value == 'terminated', f'Expected terminated, got {attr.value}'

    # -------------------------------------------------------------------
    # Step 3: MQ matcher triggers replanning with terminated context.
    # PDP.generative with employment_status='terminated' → no rule matches
    # → empty desired state → revoke diff for all current grants.
    #
    # We patch fetch_current_effective_grants and count_current_effective_grants
    # to return the fake grants seeded above (bypassing EffectiveGrant table
    # which requires complex Application/Resource FK setup).
    # -------------------------------------------------------------------
    revoke_run_id = uuid.uuid4()
    fake_revoke_run = MagicMock()
    fake_revoke_run.id = revoke_run_id
    fake_revoke_run.status = PipelineRunStatus.pending
    fake_revoke_run.pipeline_name = 'access_apply_pipeline'
    fake_revoke_run.pipeline_version = 1

    orchestrator_revoke = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator_revoke.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_revoke_run, created=True)

    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    app_revoke = _make_test_app(
        session_factory,
        orchestrator=orchestrator_revoke,
        pipelines=pipelines,
        rule_pack=_G5_RULES,
    )

    with (
        patch(
            'src.engines.access_plan.service.fetch_current_effective_grants',
            return_value=fake_grants,
        ),
        patch(
            'src.engines.access_plan.service.count_current_effective_grants',
            return_value=len(fake_grants),
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app_revoke), base_url='http://testserver') as client:
            resp_replan = await client.post('/api/v0/plans', json={'subject_ref': subject_ref})

    assert resp_replan.status_code == 201, f'Replan failed: {resp_replan.text}'
    revoke_plan_data = resp_replan.json()
    revoke_plan_id = uuid.UUID(revoke_plan_data['id'])

    assert revoke_plan_data['status'] == 'active', f'Revoke plan must be active, got {revoke_plan_data["status"]}'
    assert revoke_plan_data['subject_ref'] == subject_ref

    # -------------------------------------------------------------------
    # Step 4: Verify revoke plan properties
    # -------------------------------------------------------------------
    revoke_items_data = revoke_plan_data.get('items', [])
    # With 2 fake grants → 2 remove_fact diff items → revoke items
    assert len(revoke_items_data) >= 1, f'Expected ≥1 revoke items, got {len(revoke_items_data)}'

    # All items must be revoke operations
    revoke_op_kinds = {
        PlanItemKind.revoke_role.value,
        PlanItemKind.account_suspend.value,
        PlanItemKind.account_disable.value,
        PlanItemKind.group_remove.value,
        PlanItemKind.entitlement_detach.value,
    }
    non_revoke = {i['kind'] for i in revoke_items_data} - revoke_op_kinds
    assert not non_revoke, f'Revoke plan must only contain revoke operations, found non-revoke: {non_revoke}'

    # requires_confirmation must be True (2 revokes out of 2 total = 100% > 50% threshold)
    assert revoke_plan_data['requires_confirmation'] is True, (
        'Plan with 100% revoke diff must require confirmation (safe revoke threshold exceeded)'
    )

    # -------------------------------------------------------------------
    # Step 5: POST /plans/{id}/apply WITHOUT confirm_destructive → 422
    # -------------------------------------------------------------------
    async with AsyncClient(transport=ASGITransport(app=app_revoke), base_url='http://testserver') as client:
        resp_no_confirm = await client.post(
            f'/api/v0/plans/{revoke_plan_id}/apply',
            json={},
        )

    assert resp_no_confirm.status_code == 422, (
        f'Expected 422 for destructive plan without confirm, got {resp_no_confirm.status_code}: {resp_no_confirm.text}'
    )
    no_confirm_detail = resp_no_confirm.json().get('detail', {})
    if isinstance(no_confirm_detail, dict):
        assert no_confirm_detail.get('code') == 'destructive_threshold_exceeded', (
            f'Expected destructive_threshold_exceeded, got: {no_confirm_detail}'
        )

    # -------------------------------------------------------------------
    # Step 6: POST /plans/{id}/apply WITH confirm_destructive=true → 201
    # -------------------------------------------------------------------
    async with session_factory() as session:
        pipeline_run = PipelineRun(
            id=revoke_run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(revoke_plan_id)},
            content_hash='g5_revoke_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app_revoke), base_url='http://testserver') as client:
        resp_apply = await client.post(
            f'/api/v0/plans/{revoke_plan_id}/apply',
            json={'confirm_destructive': True},
        )

    assert resp_apply.status_code == 201, (
        f'Expected 201 with confirm_destructive, got {resp_apply.status_code}: {resp_apply.text}'
    )
    apply_data = resp_apply.json()
    assert 'pipeline_run_id' in apply_data
    returned_run_id = uuid.UUID(apply_data['pipeline_run_id'])
    assert returned_run_id == revoke_run_id

    # Verify lease created
    async with session_factory() as session:
        lease_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease = lease_result.scalar_one_or_none()
    assert lease is not None, 'AccessApplyActive lease must exist after apply'
    assert lease.plan_id == revoke_plan_id

    # -------------------------------------------------------------------
    # Step 7: Execute revoke plan (simulate pipeline worker)
    # -------------------------------------------------------------------
    async with session_factory() as session:
        revoke_items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == revoke_plan_id))
        revoke_db_items = list(revoke_items_result.scalars().all())

    n_revoke = len(revoke_db_items)
    assert n_revoke >= 1, 'Expected at least 1 revoke item in DB'

    stub_connector = _G5StubConnector()
    stub_connector.set_item_count(n_revoke)
    stub_sync = _G5StubSyncService()
    initiative_service = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        counts = await execute_plan(
            session,
            revoke_plan_id,
            revoke_run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=initiative_service,
        )

    assert counts.items_done == n_revoke, f'Expected {n_revoke} revoke items done, got {counts.items_done}'
    assert counts.items_failed == 0, f'Expected 0 failed, got {counts.items_failed}'

    # -------------------------------------------------------------------
    # Step 8: Verify post-conditions
    # -------------------------------------------------------------------

    # (a) All PlanItemExecution.status == done
    async with session_factory() as session:
        exec_result = await session.execute(
            sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == revoke_plan_id)
        )
        executions = list(exec_result.scalars().all())

    assert len(executions) == n_revoke, f'Expected {n_revoke} execution rows, got {len(executions)}'
    for exec_row in executions:
        assert exec_row.status == PlanItemExecutionStatus.done, (
            f'Item {exec_row.item_id} status={exec_row.status}, expected done'
        )

    # (b) sync_single_fact called with op=revoke for each revoke item
    from src.engines.inventory_sync.schemas import SingleFactSyncOp  # noqa: PLC0415

    revoke_sync_calls = [c for c in stub_sync.calls if c['op'] == SingleFactSyncOp.revoke]
    assert len(revoke_sync_calls) == n_revoke, f'Expected {n_revoke} revoke sync calls, got {len(revoke_sync_calls)}'

    # (c) Initiatives with non-null initiative_refs in revoke items must be closed.
    # Collect all initiative_refs from revoke items.
    all_initiative_refs: list[uuid.UUID] = []
    for db_item in revoke_db_items:
        refs = db_item.initiative_refs or []
        all_initiative_refs.extend(uuid.UUID(r) for r in refs if r)

    if all_initiative_refs:
        # Verify closed (valid_until set) and NOT deleted (audit trail preserved)
        async with session_factory() as session:
            closed_result = await session.execute(
                sa.select(Initiative).where(
                    Initiative.id.in_(all_initiative_refs),
                )
            )
            closed_initiatives = list(closed_result.scalars().all())

        assert len(closed_initiatives) == len(all_initiative_refs), (
            'All referenced initiatives must still exist in DB (NO DELETE — audit trail)'
        )
        for init in closed_initiatives:
            assert init.valid_until is not None, f'Initiative {init.id} must be closed (valid_until set) after revoke'

    # (d) access_apply_active lease released after execute_plan
    async with session_factory() as session:
        lease_after_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease_after = lease_after_result.scalar_one_or_none()
    assert lease_after is None, 'AccessApplyActive lease must be released after execute_plan'

    # (e) Revoke plan remains active after its own apply
    async with session_factory() as session:
        revoke_plan_after_result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == revoke_plan_id))
        revoke_plan_after = revoke_plan_after_result.scalar_one_or_none()

    assert revoke_plan_after is not None
    assert revoke_plan_after.status == AccessPlanStatus.active, (
        f'Revoke plan must remain active after apply, got {revoke_plan_after.status}'
    )

    # (f) Seeded initiatives still exist (NO DELETE semantic — audit trail)
    async with session_factory() as session:
        all_init_result = await session.execute(sa.select(Initiative).where(Initiative.subject_ref == subject_ref))
        all_initiatives_for_subject = list(all_init_result.scalars().all())

    assert len(all_initiatives_for_subject) >= len(initiative_ids), (
        'Original initiatives must NOT be deleted (audit trail preserved)'
    )


@pytest.mark.asyncio
async def test_terminated_empty_grants_no_confirmation(session_factory) -> None:  # type: ignore[no-untyped-def]
    """E2E G5 edge case: terminated employee with no prior grants.

    When there are no effective grants and employment_status='terminated',
    the diff is empty (desired=empty, current=empty → 0 revoke items).
    requires_confirmation must be False (0 revokes out of 0 total = 0% ≤ 50%).
    """
    # Seed fresh terminated employee (no prior grants)
    async with session_factory() as session:
        org = OrgUnit(
            external_id=f'eng-g5-empty-{uuid.uuid4().hex[:4]}',
            name='Engineering G5 Empty',
        )
        session.add(org)
        await session.flush()
        org_id = org.id

        person = await create_person(
            session,
            external_id=f'emp-g5-empty-{uuid.uuid4().hex[:6]}',
            full_name='G5 Empty Employee',
        )
        await session.flush()

        emp = await create_employee(session, person_id=person.id)
        emp.org_unit_id = org_id
        await session.flush()

        attr = EmployeeAttribute(employee_id=emp.id, key='employment_status', value='terminated')
        session.add(attr)

        subj = Subject(
            external_id=f'subj-g5-empty-{uuid.uuid4().hex[:6]}',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        session.add(subj)
        await session.flush()

        conn = ConnectorInstance(
            instance_id=f'mock-app-g5-empty-{uuid.uuid4().hex[:4]}',
            tags=['mock', 'g5', 'empty'],
        )
        session.add(conn)
        await session.commit()

    subject_ref_empty = str(subj.id)

    run_id = uuid.uuid4()
    fake_run = MagicMock()
    fake_run.id = run_id
    fake_run.status = PipelineRunStatus.pending

    orchestrator_mock = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator_mock.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_run, created=True)

    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    app = _make_test_app(
        session_factory,
        orchestrator=orchestrator_mock,
        pipelines=pipelines,
        rule_pack=_G5_RULES,
    )

    # No effective grants (empty DB) — no patching needed
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        resp = await client.post('/api/v0/plans', json={'subject_ref': subject_ref_empty})

    assert resp.status_code == 201, f'Plan creation failed: {resp.text}'
    plan_data = resp.json()

    # No items → no confirmation needed
    items = plan_data.get('items', [])
    if not items:
        assert plan_data['requires_confirmation'] is False, 'Empty plan (0 revoke items) must not require confirmation'
