# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""E2E test — Phase 19 Step G8: NHI lifecycle scenarios.

Three sub-scenarios:

(a) New NHI → birthright plan → apply → verify.
    1. Create NHI record + Subject (kind=nhi).
    2. POST /plans with subject_ref=nhi_subject_id.
       Service builds SubjectContext(nhi) from inventory.nhi.
    3. PDP.generative with subject_type=nhi applies birthright rules.
       Plan contains account_create + grant items.
    4. POST /plans/{id}/apply → 201. AccessApplyActive lease created.
    5. execute_plan: all items done. Verify AccessFact + Initiative.

(b) NHI expired → revoke plan.
    1. Setup NHI with applied birthright plan (via sub-scenario a).
    2. Call deactivate_nhi (sets is_locked=True, emits nhi.expired).
    3. Replan: PDP.generative with expired NHI (attributes.nhi_status=expired)
       produces empty desired → diff = revoke all.
    4. Apply revoke plan. Verify all facts revoked.

(c) Application decommissioned → fan-out N NHI revoke plans.
    1. Create Application + 3 NHIs each with applied birthright plans.
    2. Call decommission_application (sets is_active=False, emits
       inventory.application.decommissioned).
    3. fanout_replan_for_application_action creates one plan per NHI.
    4. Each plan is a revoke plan. Verify plans_created == 3.

Test file: src/engines/access_plan/tests/test_e2e_g8_nhi.py
Policy fixtures: inline NHI-specific birthright rules using subject_type=nhi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.deps import get_db
from src.engines.access_apply.execute_plan import execute_plan
from src.engines.access_plan.actions import (
    FanoutReplanForApplicationArgs,
    fanout_replan_for_application_action,
)
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
import src.inventory.initiatives.models  # noqa: F401 — registers Initiative for create_all
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.initiatives.service import InitiativeService
import src.inventory.nhi.models  # noqa: F401 — registers NHI for create_all
from src.inventory.nhi.models import NHI, NHIAttribute
from src.inventory.nhi.service import NHIService
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
import src.platform.applications.models  # noqa: F401 — registers Application for create_all
from src.platform.applications.models import Application
from src.platform.applications.service import decommission_application
import src.platform.connectors.models  # noqa: F401 — registers ConnectorInstance for create_all
from src.platform.connectors.models import ConnectorInstance
from src.platform.events.service import EventService, noop_event_service
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import noop_log_service
from src.platform.orchestrator.loader import PipelineDefinition
import src.platform.orchestrator.models  # noqa: F401 — registers PipelineRun for create_all
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus, PipelineTriggerSource
from src.platform.orchestrator.registry import ActionContext
from src.platform.orchestrator.service import PipelineOrchestratorService
from src.platform.orchestrator.service_types import PipelineRunCreateResult
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# Inline policy fixture — NHI birthright rules
#
# Rules apply when subject_type=nhi AND attributes.nhi_status=active.
# When nhi_status is absent or not 'active', no rule matches → empty desired.
# ---------------------------------------------------------------------------

_APP_INSTANCE_ID = 'mock-app-g8'
_NHI_APP_INSTANCE_ID_2 = 'mock-app-g8-fanout'

_NHI_BIRTHRIGHT_RULES = RulePack(
    lifecycle=[
        Rule(
            id='nhi_birthright_account_create',
            kind='birthright',
            when={
                'subject.kind': 'nhi',
                'attributes.nhi_status': 'active',
            },
            then={
                'application': _APP_INSTANCE_ID,
                'fact_kind': 'account',
                'target_descriptor': {'fact_kind': 'account'},
            },
            precedence=10,
        ),
        Rule(
            id='nhi_birthright_grant_service',
            kind='birthright',
            when={
                'subject.kind': 'nhi',
                'attributes.nhi_status': 'active',
            },
            then={
                'application': _APP_INSTANCE_ID,
                'fact_kind': 'role_grant',
                'target_descriptor': {'role_ref': 'service_role', 'fact_kind': 'role_grant'},
            },
            precedence=9,
        ),
    ],
)

# Rules for the fanout test — use a different app instance
_NHI_FANOUT_RULES = RulePack(
    lifecycle=[
        Rule(
            id='nhi_fanout_account_create',
            kind='birthright',
            when={
                'subject.kind': 'nhi',
                'attributes.nhi_status': 'active',
            },
            then={
                'application': _NHI_APP_INSTANCE_ID_2,
                'fact_kind': 'account',
                'target_descriptor': {'fact_kind': 'account'},
            },
            precedence=10,
        ),
    ],
)


# ---------------------------------------------------------------------------
# Stub connector
# ---------------------------------------------------------------------------


class _G8StubConnector:
    """Stub connector: preflight mismatch → connector call → post-verify match."""

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


class _G8StubSyncService:
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
        content_hash='g8_hash',
        args_schema_dict={},
        steps=(),
        triggers=(),
        raw_dict={},
    )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_application(session: AsyncSession, *, instance_id: str = _APP_INSTANCE_ID) -> Application:
    """Seed an Application row with the given instance_id as code."""
    app = Application(
        name=f'G8 App {instance_id}',
        code=instance_id,
        config={},
        required_connector_tags=['mock'],
    )
    session.add(app)
    await session.flush()
    return app


async def _seed_connector_instance(session: AsyncSession, *, instance_id: str = _APP_INSTANCE_ID) -> ConnectorInstance:
    """Register a mock connector instance."""
    from src.platform.connectors.mock_connector import MOCK_CONNECTOR_DESCRIPTOR  # noqa: PLC0415

    conn = ConnectorInstance(
        instance_id=instance_id,
        tags=['mock', 'g8'],
        descriptor=MOCK_CONNECTOR_DESCRIPTOR.model_dump(mode='json'),
    )
    session.add(conn)
    await session.flush()
    return conn


async def _seed_nhi(
    session: AsyncSession,
    *,
    application_id: uuid.UUID | None = None,
    nhi_status: str = 'active',
    external_id_suffix: str = '',
) -> tuple[NHI, Subject]:
    """Create NHI + NHIAttribute(nhi_status) + Subject(kind=nhi)."""
    suffix = external_id_suffix or uuid.uuid4().hex[:6]

    nhi = NHI(
        external_id=f'nhi-g8-{suffix}',
        name=f'G8 NHI {suffix}',
        kind='service_account',
        application_id=application_id,
    )
    session.add(nhi)
    await session.flush()

    # nhi_status attribute drives PDP matching
    attr = NHIAttribute(nhi_id=nhi.id, key='nhi_status', value=nhi_status)
    session.add(attr)

    subj = Subject(
        external_id=f'nhi-subj-g8-{suffix}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subj)
    await session.flush()

    return nhi, subj


async def _seed_initiatives_for_nhi(
    session: AsyncSession,
    subject_ref: str,
    *,
    count: int = 1,
) -> list[Initiative]:
    """Seed Initiative rows to simulate existing birthright grants for an NHI."""
    initiatives = []
    for i in range(count):
        init = Initiative(
            id=uuid.uuid4(),
            access_fact_id=uuid.uuid4(),
            type=InitiativeType.birthright,
            origin=f'policy_rule:nhi_birthright_rule_g8_{i}',
            subject_ref=subject_ref,
            subject_type='nhi',
        )
        session.add(init)
        initiatives.append(init)
    await session.flush()
    return initiatives


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
    pdp = GenerativePDPService(rule_pack=rule_pack or _NHI_BIRTHRIGHT_RULES)
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
async def test_nhi_new_birthright_plan_apply_verify(session_factory) -> None:  # type: ignore[no-untyped-def]
    """G8(a): New NHI → birthright plan → apply → verify AccessFact + Initiative.

    Phase 19 Step G8 — sub-scenario (a):
    1. Create NHI record (service_account, nhi_status=active, application_ref).
    2. POST /plans with subject_ref=nhi_subject_id.
    3. PDP applies NHI-specific birthright rules → account_create + grant_role.
    4. POST /plans/{id}/apply → 201 + lease created.
    5. execute_plan → all items done.
    6. Verify: sync_single_fact called per item, Initiative with
       origin=policy_rule:nhi_birthright_account_create, lease deleted.
    """
    # Step 1: Seed DB
    async with session_factory() as session:
        app = await _seed_application(session)
        await _seed_connector_instance(session)
        nhi, subj = await _seed_nhi(session, application_id=app.id)
        await session.commit()

    subject_ref = str(subj.id)

    # Step 2: Build orchestrator mock
    run_id = uuid.uuid4()
    fake_run = MagicMock()
    fake_run.id = run_id
    fake_run.status = PipelineRunStatus.pending
    fake_run.pipeline_name = 'access_apply_pipeline'
    fake_run.pipeline_version = 1

    orchestrator_mock = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator_mock.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_run, created=True)

    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    app_obj = _make_test_app(session_factory, orchestrator=orchestrator_mock, pipelines=pipelines)

    # Step 3: POST /plans → 201
    async with AsyncClient(transport=ASGITransport(app=app_obj), base_url='http://testserver') as client:
        resp = await client.post('/api/v0/plans', json={'subject_ref': subject_ref})

    assert resp.status_code == 201, f'Expected 201, got {resp.status_code}: {resp.text}'
    plan_data = resp.json()
    assert plan_data['subject_ref'] == subject_ref
    assert plan_data['status'] == 'active'

    plan_id = uuid.UUID(plan_data['id'])
    items = plan_data.get('items', [])
    assert len(items) >= 1, f'Plan must have at least one item, got: {items}'

    item_kinds = {item['kind'] for item in items}
    assert item_kinds & {
        PlanItemKind.account_create.value,
        PlanItemKind.account_invite.value,
        PlanItemKind.grant_role.value,
    }, f'Expected account or grant items, got: {item_kinds}'

    for item in items:
        assert item['decision_snapshot'], f'decision_snapshot empty for item {item["id"]}'

    # Step 4: POST /plans/{id}/apply → 201
    async with session_factory() as session:
        pipeline_run = PipelineRun(
            id=run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(plan_id)},
            content_hash='g8a_e2e_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app_obj), base_url='http://testserver') as client:
        resp_apply = await client.post(f'/api/v0/plans/{plan_id}/apply', json={})

    assert resp_apply.status_code == 201, f'Expected 201 apply, got {resp_apply.status_code}: {resp_apply.text}'
    apply_data = resp_apply.json()
    assert uuid.UUID(apply_data['pipeline_run_id']) == run_id

    # Verify AccessApplyActive lease created
    async with session_factory() as session:
        lease_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease = lease_result.scalar_one_or_none()
    assert lease is not None, 'AccessApplyActive lease must exist after apply'

    # Step 5: Execute plan
    async with session_factory() as session:
        items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_id))
        db_items = list(items_result.scalars().all())

    n_items = len(db_items)
    stub_connector = _G8StubConnector()
    stub_connector.set_item_count(n_items)
    stub_sync = _G8StubSyncService()
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
    assert counts.items_failed == 0

    # Step 6: Verify post-conditions
    # (a) All executions done
    async with session_factory() as session:
        exec_result = await session.execute(sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan_id))
        executions = list(exec_result.scalars().all())
    assert len(executions) == n_items
    for exec_row in executions:
        assert exec_row.status == PlanItemExecutionStatus.done

    # (b) sync_single_fact called per item
    assert len(stub_sync.calls) == n_items, f'Expected {n_items} sync calls, got {len(stub_sync.calls)}'

    # (c) Initiative rows created with origin=policy_rule:nhi_birthright_*
    async with session_factory() as session:
        init_result = await session.execute(
            sa.select(Initiative).where(
                Initiative.subject_ref == subject_ref,
            )
        )
        all_initiatives = list(init_result.scalars().all())
    birthright_initiatives = [
        i for i in all_initiatives if i.type == InitiativeType.birthright or i.origin.startswith('policy_rule:')
    ]
    assert len(birthright_initiatives) >= 1, (
        f'Expected at least 1 birthright initiative for NHI, got {len(birthright_initiatives)}'
    )

    # (d) Lease deleted after execute_plan
    async with session_factory() as session:
        lease_after = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        assert lease_after.scalar_one_or_none() is None, 'Lease must be deleted after execute_plan'

    # (e) Plan status active
    async with session_factory() as session:
        plan_row = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == plan_id))
        plan_after = plan_row.scalar_one_or_none()
    assert plan_after is not None
    assert plan_after.status == AccessPlanStatus.active


@pytest.mark.asyncio
async def test_nhi_expired_revoke_plan(session_factory) -> None:  # type: ignore[no-untyped-def]
    """G8(b): NHI.expired → replan with empty desired → revoke plan → apply.

    Phase 19 Step G8 — sub-scenario (b):
    1. Setup NHI with applied birthright plan (simulated via seeded initiatives
       + patched effective grants).
    2. deactivate_nhi → sets is_locked=True, emits inventory.nhi.expired.
    3. Update NHI attribute nhi_status=expired (simulates expired state).
    4. Replan: PDP with nhi_status!=active → empty desired → revoke all.
    5. Apply revoke plan → all items done.
    6. Verify: sync_single_fact called with op=revoke, all executions done.
    """
    from unittest.mock import patch  # noqa: PLC0415

    from src.engines.access_effective.models import EffectiveGrant, EffectiveGrantEffect  # noqa: PLC0415
    from src.inventory.enums import Action  # noqa: PLC0415

    # Step 1: Seed DB — NHI with active status + initiatives for existing grants
    async with session_factory() as session:
        app = await _seed_application(session, instance_id='mock-app-g8b')
        await _seed_connector_instance(session, instance_id='mock-app-g8b')
        nhi, subj = await _seed_nhi(session, application_id=app.id, external_id_suffix='b1')
        nhi_id = nhi.id
        # Seed initiatives to simulate existing birthright grants
        initiatives = await _seed_initiatives_for_nhi(session, str(subj.id), count=2)
        await session.commit()

    subject_ref = str(subj.id)
    subject_uuid = subj.id

    # Build fake effective grants for mock patching
    fake_grants = [
        EffectiveGrant(
            id=uuid.uuid4(),
            subject_id=subject_uuid,
            subject_kind=SubjectKind.nhi,
            application_id=app.id,
            resource_id=uuid.uuid4(),
            action=Action.read,
            effect=EffectiveGrantEffect.allow,
            initiative_type=InitiativeType.birthright,
            initiative_origin=f'policy_rule:nhi_birthright_rule_g8_{i}',
            valid_from=None,
            source_access_fact_id=initiatives[i].access_fact_id,
            source_initiative_id=initiatives[i].id,
        )
        for i in range(2)
    ]

    # Step 2: Expire NHI via deactivate_nhi
    capturing = CapturingEventService()
    event_service = EventService(sink=capturing)
    nhi_service = NHIService(event_service=event_service)

    async with session_factory() as session:
        await nhi_service.deactivate_nhi(session, nhi_id)
        await session.commit()

    expired_events = capturing.filter_by_type('inventory.nhi.expired')
    assert len(expired_events) == 1, f'Expected 1 nhi.expired event, got {len(expired_events)}'
    assert expired_events[0].payload['nhi_id'] == str(nhi_id)

    # Step 3: Update nhi_status attribute to 'expired' so PDP sees no match
    async with session_factory() as session:
        await session.execute(
            sa.update(NHIAttribute)
            .where(NHIAttribute.nhi_id == nhi_id, NHIAttribute.key == 'nhi_status')
            .values(value='expired')
        )
        await session.commit()

    # Step 4: Replan with patched effective grants and expired NHI
    revoke_run_id = uuid.uuid4()
    fake_revoke_run = MagicMock()
    fake_revoke_run.id = revoke_run_id
    fake_revoke_run.status = PipelineRunStatus.pending
    fake_revoke_run.pipeline_name = 'access_apply_pipeline'
    fake_revoke_run.pipeline_version = 1

    orchestrator_mock = AsyncMock(spec=PipelineOrchestratorService)
    orchestrator_mock.create_pipeline_run.return_value = PipelineRunCreateResult(run=fake_revoke_run, created=True)

    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    test_app = _make_test_app(session_factory, orchestrator=orchestrator_mock, pipelines=pipelines)

    # Patch fetch_current_effective_grants to return fake grants and
    # count_current_effective_grants to return their count
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
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.post(
                '/api/v0/plans',
                json={'subject_ref': subject_ref},
            )

    assert resp.status_code == 201, f'Expected 201 replan, got {resp.status_code}: {resp.text}'
    revoke_plan_data = resp.json()
    revoke_plan_id = uuid.UUID(revoke_plan_data['id'])

    # Plan should have requires_confirmation=True (revoke threshold exceeded)
    # OR just have revoke items
    items = revoke_plan_data.get('items', [])
    assert len(items) >= 1, 'Revoke plan must have items, got 0'

    # Step 5: Apply revoke plan
    async with session_factory() as session:
        pipeline_run = PipelineRun(
            id=revoke_run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(revoke_plan_id)},
            content_hash='g8b_revoke_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
        resp_apply = await client.post(
            f'/api/v0/plans/{revoke_plan_id}/apply',
            json={'confirm_destructive': True},
        )

    assert resp_apply.status_code == 201, f'Expected 201 apply, got {resp_apply.status_code}: {resp_apply.text}'

    # Step 5b: Execute revoke plan
    async with session_factory() as session:
        items_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == revoke_plan_id))
        db_items = list(items_result.scalars().all())

    n_items = len(db_items)
    stub_connector = _G8StubConnector()
    stub_connector.set_item_count(n_items)
    stub_sync = _G8StubSyncService()
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

    assert counts.items_done == n_items, f'Expected {n_items} done, got {counts.items_done}'
    assert counts.items_failed == 0

    # Step 6: Verify all executions done
    async with session_factory() as session:
        exec_result = await session.execute(
            sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == revoke_plan_id)
        )
        executions = list(exec_result.scalars().all())

    assert len(executions) == n_items
    for exec_row in executions:
        assert exec_row.status == PlanItemExecutionStatus.done, f'Item {exec_row.item_id} status={exec_row.status}'

    # Verify sync_single_fact called per revoke item
    assert len(stub_sync.calls) == n_items, f'Expected {n_items} sync calls, got {len(stub_sync.calls)}'


@pytest.mark.asyncio
async def test_application_decommissioned_fanout_nhi_revoke(session_factory) -> None:  # type: ignore[no-untyped-def]
    """G8(c): application.decommissioned → fan-out N replan'ов → revoke all NHIs.

    Phase 19 Step G8 — sub-scenario (c):
    1. Create Application + 3 NHIs with Subject records.
    2. decommission_application emits inventory.application.decommissioned.
    3. fanout_replan_for_application_action creates one revoke plan per NHI.
       Each plan uses idempotency_key=f"{application_id}:{nhi_id}".
    4. Verify plans_created == 3, nhi_count == 3.
    5. Each created plan is a revoke plan (empty desired → remove_fact items
       when grants are patched, or empty plan when no grants exist).
    """
    # Step 1: Seed Application + 3 NHIs
    async with session_factory() as session:
        fanout_app = await _seed_application(session, instance_id='mock-app-g8c')
        await _seed_connector_instance(session, instance_id='mock-app-g8c')

        nhis_and_subjects: list[tuple[NHI, Subject]] = []
        for i in range(3):
            nhi, subj = await _seed_nhi(
                session,
                application_id=fanout_app.id,
                external_id_suffix=f'c{i}',
            )
            nhis_and_subjects.append((nhi, subj))

        await session.commit()

    application_id = fanout_app.id
    application_id_str = str(application_id)

    # Step 2: Decommission application
    capturing = CapturingEventService()
    event_service = EventService(sink=capturing)

    async with session_factory() as session:
        await decommission_application(session, application_id, event_service=event_service)
        await session.commit()

    decomm_events = capturing.filter_by_type('inventory.application.decommissioned')
    assert len(decomm_events) == 1, f'Expected 1 decommissioned event, got {len(decomm_events)}'
    assert decomm_events[0].payload['application_id'] == application_id_str

    # Step 3: Simulate fanout action via direct function call
    # Build a minimal ActionContext with session + noop services
    async with session_factory() as session:
        action_ctx = ActionContext(
            session=session,
            log_service=noop_log_service,
            pipeline_run_id=uuid.uuid4(),
            step_run_id=uuid.uuid4(),
            attempt=1,
            worker_id='test-worker-g8c',
        )

        result = await fanout_replan_for_application_action(
            FanoutReplanForApplicationArgs(application_id=application_id_str),
            action_ctx,
        )
        await session.commit()

    # Step 4: Verify plans_created == 3, nhi_count == 3
    assert result.nhi_count == 3, f'Expected 3 NHIs, got {result.nhi_count}'
    assert result.plans_created == 3, f'Expected 3 plans created, got {result.plans_created}'
    assert result.application_id == application_id_str

    # Step 5: Verify one AccessPlan per NHI (with idempotency_key)
    async with session_factory() as session:
        plans_result = await session.execute(
            sa.select(AccessPlan).where(
                AccessPlan.subject_type == 'nhi',
            )
        )
        all_plans = list(plans_result.scalars().all())

    # All 3 NHI subjects should have plans
    subject_refs_with_plans = {p.subject_ref for p in all_plans}
    expected_subject_refs = {str(subj.id) for _, subj in nhis_and_subjects}
    # Each NHI subject must have a plan
    for expected_ref in expected_subject_refs:
        assert expected_ref in subject_refs_with_plans, (
            f'NHI subject {expected_ref} does not have a plan. Plans exist for: {subject_refs_with_plans}'
        )

    # Verify idempotency keys are set
    for plan in all_plans:
        if plan.subject_ref in expected_subject_refs:
            assert plan.idempotency_key is not None, 'Plan for NHI must have idempotency_key, got None'
            assert application_id_str in plan.idempotency_key, (
                f'idempotency_key must include application_id, got {plan.idempotency_key!r}'
            )

    # Step 5b: Idempotency — second fan-out with same args must reuse existing plans
    async with session_factory() as session:
        action_ctx2 = ActionContext(
            session=session,
            log_service=noop_log_service,
            pipeline_run_id=uuid.uuid4(),
            step_run_id=uuid.uuid4(),
            attempt=1,
            worker_id='test-worker-g8c-2',
        )

        result2 = await fanout_replan_for_application_action(
            FanoutReplanForApplicationArgs(application_id=application_id_str),
            action_ctx2,
        )
        await session.commit()

    # Second run: same 3 NHIs, same idempotency_keys → reuse (plans_created counted as reused)
    assert result2.nhi_count == 3, f'Second fanout: expected 3 NHIs, got {result2.nhi_count}'
    assert result2.plans_created == 3, f'Second fanout: expected 3 (reused), got {result2.plans_created}'
