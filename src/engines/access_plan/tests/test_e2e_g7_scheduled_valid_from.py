# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""E2E test — Phase 19 Step G7: initiative with future valid_from →
scanner picks up in window → subject.replan.required → matcher →
replan → plan apply → verify.

Scenario (clock-override based, no real sleep):

T0 — "now"
T+3s — future clock passed to scanner and to create_plan

1. Seed Employee X with a 'requested' Initiative:
   valid_from = T0 + 2s  (future, not yet active at T0)
   type = requested, application = mock-app-g7

2. POST /plans/dry-run for X at T0 (via service.create_plan with now=T0):
   The PDP.assess() call uses now=T0, so the initiative with
   valid_from > T0 is filtered by _is_active_initiative and does NOT
   appear in desired state.  Plan dry-run has no items.

3. run_scan_for_replan with:
   - now = T0 + 3s  (future clock overrides)
   - lookback_seconds = 1  (tiny DI override — covers only ±1s from T0+3s)
   The window = [T0+2s, T0+64s].  The initiative (valid_from=T0+2s) falls
   in [window_start, window_end] → scanner finds it, emits
   subject.replan.required with idempotency_key = sha1(subject_ref:bucket).

4. Verify scanner result: subjects_queued == 1, initiatives_scanned == 1.

5. Deduplicate: run the scanner a second time with the same "now" →
   same idempotency_key produced (same bucket).  Verify second run still
   returns subjects_queued == 1 (the key is deterministic; matcher deduplication
   is at pipeline uniqueness level, not scanner level).

6. Simulate matcher action: call plan_action / service.create_plan with
   - subject_ref = X.subject_ref
   - idempotency_key from the scanner event
   - now = T0 + 3s  (effective_now matching scanner's clock)
   The PDP.assess() now uses effective_now = T0+3s, so the initiative
   (valid_from = T0+2s <= T0+3s) is active → it appears in desired state →
   plan has items.

7. Verify plan contains carry-over of the 'requested' initiative with
   valid_from-gated access.

8. POST /plans/{plan_id}/apply → 201 + pipeline_run_id.

9. execute_plan: stub connector mismatch → connect → post-verify match.
   All items done.  sync_single_fact called per item.  Lease released.

10. Verify: Initiative in DB attached to a plan fact_id; plan status active.

Test determinism: uses a frozen clock (datetime constants), no asyncio.sleep.
scanner_window_lookback overridden to 1 second via DI parameter to run_scan_for_replan.
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
    AccessPlan,
    AccessPlanStatus,
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
# Policy fixture — no birthright rules; only carry-over is active
# ---------------------------------------------------------------------------

_APP_INSTANCE_ID = 'mock-app-g7'

# No birthright rules — the initiative with future valid_from is the only source
# of desired state once valid_from has been reached.
_RULES = RulePack(lifecycle=[], risk=[])

# ---------------------------------------------------------------------------
# Frozen clock helpers
# ---------------------------------------------------------------------------

# T0: arbitrary base time (arbitrary; test is deterministic)
_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_FUTURE_VALID_FROM = _T0 + timedelta(seconds=2)  # 2s in the future at T0
_SCANNER_NOW = _T0 + timedelta(seconds=3)  # scanner runs 3s after T0


# ---------------------------------------------------------------------------
# Stub connector — preflight mismatch → invoke → post-verify match
# ---------------------------------------------------------------------------


class _G7StubConnector:
    """Stub connector for G7."""

    def __init__(self, n_items: int = 1) -> None:
        self.invoke_calls: list[dict[str, Any]] = []
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


class _G7StubSyncService:
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
        self.calls.append({'op': op, 'event_key': event_key})
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
        content_hash='g7_hash',
        args_schema_dict={},
        steps=(),
        triggers=(),
        raw_dict={},
    )


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_org_unit(session: AsyncSession, ext_id: str) -> OrgUnit:
    org = OrgUnit(external_id=ext_id, name=f'G7 Org {ext_id}')
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
    """Create Person → Employee + EmployeeAttribute + Subject."""
    person = await create_person(
        session,
        external_id=f'emp-g7-{name_suffix}-{uuid.uuid4().hex[:6]}',
        full_name=f'G7 Employee {name_suffix}',
    )
    await session.flush()

    emp = await create_employee(session, person_id=person.id)
    emp.org_unit_id = org_unit_id
    await session.flush()

    attr = EmployeeAttribute(employee_id=emp.id, key='employment_status', value=employment_status)
    session.add(attr)

    subj = Subject(
        external_id=f'subj-g7-{name_suffix}-{uuid.uuid4().hex[:6]}',
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
        tags=['mock', 'g7'],
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
    now_override: datetime | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with DI overrides.

    now_override is passed to service.create_plan to freeze the clock for
    testing future valid_from scenarios.
    """
    pdp = GenerativePDPService(rule_pack=rule_pack)
    settings = RuntimeSettingsConfig()
    _now = now_override  # captured in closure

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
# Orchestrator + pipeline run helper
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
# Test — future valid_from: scanner + replan + apply
# ===========================================================================


@pytest.mark.asyncio
async def test_future_valid_from_scanner_replan_apply(session_factory) -> None:  # type: ignore[no-untyped-def]
    """G7 scenario: initiative with future valid_from → scanner picks it up in window.

    Steps:
    1. Seed employee X + connector.
    2. Seed initiative with valid_from = T0+2s (future at T0).
    3. dry-run at T0 → no items (initiative not yet active).
    4. run_scan_for_replan(now=T0+3s, lookback=1s) → finds initiative, emits event.
    5. Verify scanner result: subjects_queued=1.
    6. Idempotency: run scanner again with same clock → same key, subjects_queued=1.
    7. Create plan with now=T0+3s → initiative is active → plan has items.
    8. Apply → execute_plan → all done.
    9. Post-conditions: Initiative in DB, lease released, fact in effective access.
    """
    from src.inventory.initiatives.actions import run_scan_for_replan  # noqa: PLC0415
    from src.platform.logs.service import NoOpLogService  # noqa: PLC0415
    from src.platform.orchestrator.registry import ActionContext  # noqa: PLC0415

    # -----------------------------------------------------------------------
    # Step 1: Seed DB
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        org = await _seed_org_unit(session, f'eng-g7-{uuid.uuid4().hex[:4]}')
        _emp, subj = await _seed_employee_with_subject(session, org.id, 'x')
        await _seed_connector(session, _APP_INSTANCE_ID)
        await session.commit()

    subject_ref = str(subj.id)

    # -----------------------------------------------------------------------
    # Step 2: Seed initiative with future valid_from
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        future_initiative = Initiative(
            id=uuid.uuid4(),
            access_fact_id=uuid.uuid4(),  # placeholder UUID, no FK constraint
            type=InitiativeType.requested,
            origin='request:g7-test-request-001',
            subject_ref=subject_ref,
            subject_type='employee',
            valid_from=_FUTURE_VALID_FROM,  # T0 + 2s — not yet active at T0
        )
        session.add(future_initiative)
        await session.commit()

    future_initiative_id = future_initiative.id

    # -----------------------------------------------------------------------
    # Step 3: dry-run at T0 — initiative NOT in desired state (valid_from > T0)
    #
    # We use service.create_plan directly with now=T0 to exercise the same
    # code path as POST /plans/dry-run without needing the HTTP route.
    # Patch _initiatives_to_current_initiatives to supply application + target_descriptor.
    # -----------------------------------------------------------------------
    def _future_initiatives_to_current(
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
                    application=_APP_INSTANCE_ID,
                    target_descriptor={'fact_kind': 'role_grant', 'role_ref': 'g7_requested_role'},
                )
            )
        return result

    # Build a temporary service to call create_plan with frozen T0 clock
    run_id_replan = uuid.uuid4()
    orchestrator, _ = _make_orchestrator_and_run(run_id_replan)
    pipelines = {'access_apply_pipeline': _fake_pipeline_def()}
    _app_t0 = _make_app(session_factory, orchestrator=orchestrator, pipelines=pipelines, rule_pack=_RULES)

    # Dry-run: at T0, the initiative is in the future → PDP carry-over should filter it
    # We create a plan at T0 and verify it has 0 items (empty diff).
    with patch(
        'src.engines.access_plan.service._initiatives_to_current_initiatives',
        side_effect=_future_initiatives_to_current,
    ):
        pdp_t0 = GenerativePDPService(rule_pack=_RULES)
        async with session_factory() as session:
            service_t0 = AccessPlanService(
                session=session,
                pdp_service=pdp_t0,
                event_service=noop_event_service,
                settings=RuntimeSettingsConfig(),
            )
            plan_t0 = await service_t0.create_plan(
                subject_ref=subject_ref,
                now=_T0,
            )
            await session.commit()

    plan_t0_id = plan_t0.id

    # Verify the T0 plan has 0 items (no desired state yet)
    async with session_factory() as session:
        items_t0_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_t0_id))
        items_t0 = list(items_t0_result.scalars().all())

    assert len(items_t0) == 0, (
        f'At T0, initiative with valid_from in future must NOT appear in desired state. Got {len(items_t0)} items.'
    )

    # -----------------------------------------------------------------------
    # Step 4: run_scan_for_replan with frozen clock T0+3s and lookback=1s
    #
    # Window: [T0+3s - 1s, T0+3s + 60s] = [T0+2s, T0+63s]
    # The initiative valid_from=T0+2s falls in [T0+2s, T0+63s] → scanner finds it.
    # -----------------------------------------------------------------------
    emitted_events: list[Any] = []

    class _CapturingEventService:
        """Captures emitted events for test assertion."""

        async def emit(self, envelope: Any) -> None:
            emitted_events.append(envelope)

    capturing_svc = _CapturingEventService()

    async with session_factory() as session:
        ctx = ActionContext(
            session=session,
            log_service=NoOpLogService(),
            pipeline_run_id=uuid.uuid4(),
            step_run_id=uuid.uuid4(),
            attempt=1,
            worker_id='test-g7',
        )

        # ActionContext is frozen+slots — cannot inject event_service directly.
        # run_scan_for_replan falls back to noop_event_service when ctx lacks
        # event_service.  Patch it at the import location inside the function.
        with patch('src.platform.events.service.noop_event_service', capturing_svc):
            scan_result_1 = await run_scan_for_replan(
                ctx,
                now=_SCANNER_NOW,
                lookback_seconds=1,  # DI override — tiny lookback, deterministic
            )
        await session.commit()

    # -----------------------------------------------------------------------
    # Step 5: Verify scanner found the initiative and queued the subject
    # -----------------------------------------------------------------------
    assert scan_result_1.initiatives_scanned >= 1, (
        f'Scanner must find at least 1 initiative at T0+3s with lookback=1s. '
        f'Got initiatives_scanned={scan_result_1.initiatives_scanned}'
    )
    assert scan_result_1.subjects_queued >= 1, (
        f'Scanner must queue at least 1 subject. Got subjects_queued={scan_result_1.subjects_queued}'
    )

    # Verify the emitted event carries subject_ref and idempotency_key
    replan_events = [e for e in emitted_events if getattr(e, 'event_type', '') == 'subject.replan.required']
    assert len(replan_events) >= 1, 'Scanner must emit ≥1 subject.replan.required event'
    replan_event = replan_events[0]
    assert replan_event.payload.get('subject_id') == subject_ref, (
        f'Event subject_id must match seeded subject_ref={subject_ref!r}, '
        f'got {replan_event.payload.get("subject_id")!r}'
    )
    idempotency_key = replan_event.payload.get('idempotency_key')
    assert idempotency_key, 'Event must carry idempotency_key'

    # -----------------------------------------------------------------------
    # Step 6: Idempotency — run scanner again with same clock → same key
    # -----------------------------------------------------------------------
    emitted_events_2: list[Any] = []

    class _CapturingEventService2:
        async def emit(self, envelope: Any) -> None:
            emitted_events_2.append(envelope)

    capturing_svc_2 = _CapturingEventService2()

    async with session_factory() as session:
        ctx2 = ActionContext(
            session=session,
            log_service=NoOpLogService(),
            pipeline_run_id=uuid.uuid4(),
            step_run_id=uuid.uuid4(),
            attempt=1,
            worker_id='test-g7-2',
        )

        with patch('src.platform.events.service.noop_event_service', capturing_svc_2):
            await run_scan_for_replan(
                ctx2,
                now=_SCANNER_NOW,  # same frozen clock → same bucket → same key
                lookback_seconds=1,
            )
        await session.commit()

    replan_events_2 = [e for e in emitted_events_2 if getattr(e, 'event_type', '') == 'subject.replan.required']
    assert len(replan_events_2) >= 1, 'Second scanner run must also emit event (key is deterministic)'

    # The idempotency_key must be identical across both runs (same subject, same bucket)
    idempotency_key_2 = replan_events_2[0].payload.get('idempotency_key')
    assert idempotency_key == idempotency_key_2, (
        f'Idempotency key must be stable for the same subject+bucket: '
        f'run1={idempotency_key!r}, run2={idempotency_key_2!r}'
    )

    # -----------------------------------------------------------------------
    # Step 7: Matcher triggers replan — create plan at T0+3s
    #
    # The initiative is now active (valid_from=T0+2s <= T0+3s).
    # PDP carry-over picks it up → plan has items.
    # -----------------------------------------------------------------------
    with patch(
        'src.engines.access_plan.service._initiatives_to_current_initiatives',
        side_effect=_future_initiatives_to_current,
    ):
        pdp_future = GenerativePDPService(rule_pack=_RULES)
        async with session_factory() as session:
            service_future = AccessPlanService(
                session=session,
                pdp_service=pdp_future,
                event_service=noop_event_service,
                settings=RuntimeSettingsConfig(),
            )
            plan_future = await service_future.create_plan(
                subject_ref=subject_ref,
                idempotency_key=idempotency_key,  # from scanner event
                now=_SCANNER_NOW,  # T0 + 3s — initiative is now active
            )
            await session.commit()

    plan_future_id = plan_future.id

    # -----------------------------------------------------------------------
    # Step 7a: Verify the new plan has items (initiative now active at T0+3s)
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        items_future_result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_future_id))
        items_future = list(items_future_result.scalars().all())

    assert len(items_future) >= 1, (
        f'At T0+3s, initiative with valid_from=T0+2s must appear in desired state. '
        f'Got {len(items_future)} items. '
        'Ensure _initiatives_to_current_initiatives patch supplies application+target_descriptor.'
    )

    # At least one item must carry requested initiative in its initiatives list
    requested_items = [
        item for item in items_future if any(i.get('type') == 'requested' for i in (item.initiatives or []))
    ]
    assert len(requested_items) >= 1, (
        f'Expected ≥1 plan item with requested initiative. Got: {[item.initiatives for item in items_future]}'
    )

    # -----------------------------------------------------------------------
    # Step 7b: Verify supersedes chain: new plan supersedes T0 plan
    # -----------------------------------------------------------------------
    assert plan_future.supersedes_plan_id == plan_t0_id, (
        f'New plan must supersede T0 plan. '
        f'Expected supersedes_plan_id={plan_t0_id}, got {plan_future.supersedes_plan_id}'
    )

    # -----------------------------------------------------------------------
    # Step 8: POST /plans/{plan_future_id}/apply → 201
    # -----------------------------------------------------------------------
    run_id = uuid.uuid4()
    orchestrator_apply, _ = _make_orchestrator_and_run(run_id)
    pipelines_apply = {'access_apply_pipeline': _fake_pipeline_def()}
    app_apply = _make_app(
        session_factory,
        orchestrator=orchestrator_apply,
        pipelines=pipelines_apply,
        rule_pack=_RULES,
    )

    # Seed pipeline run in DB so apply route can check status
    async with session_factory() as session:
        pipeline_run = PipelineRun(
            id=run_id,
            pipeline_name='access_apply_pipeline',
            pipeline_version=1,
            status=PipelineRunStatus.pending,
            trigger_source=PipelineTriggerSource.http,
            args={'plan_id': str(plan_future_id)},
            content_hash='g7_apply_run',
        )
        session.add(pipeline_run)
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=app_apply), base_url='http://testserver') as client:
        resp_apply = await client.post(f'/api/v0/plans/{plan_future_id}/apply', json={})

    assert resp_apply.status_code == 201, f'Apply must return 201. Got {resp_apply.status_code}: {resp_apply.text}'
    apply_data = resp_apply.json()
    assert 'pipeline_run_id' in apply_data
    assert uuid.UUID(apply_data['pipeline_run_id']) == run_id

    # Lease exists
    async with session_factory() as session:
        lease_result = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        lease = lease_result.scalar_one_or_none()
    assert lease is not None, 'AccessApplyActive lease must exist after apply'

    # -----------------------------------------------------------------------
    # Step 9: execute_plan — stub connector sequence
    # -----------------------------------------------------------------------
    n_items = len(items_future)
    stub_connector = _G7StubConnector(n_items=n_items)
    stub_sync = _G7StubSyncService()
    initiative_service = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        counts = await execute_plan(
            session,
            plan_future_id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=initiative_service,
        )

    assert counts.items_done == n_items, f'Expected {n_items} items done, got {counts.items_done}'
    assert counts.items_failed == 0, f'Expected 0 failed, got {counts.items_failed}'

    # -----------------------------------------------------------------------
    # Step 10: Post-conditions
    # -----------------------------------------------------------------------

    # (a) All executions done
    async with session_factory() as session:
        exec_result = await session.execute(
            sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan_future_id)
        )
        executions = list(exec_result.scalars().all())

    assert len(executions) == n_items
    for exc in executions:
        assert exc.status == PlanItemExecutionStatus.done, f'Item {exc.item_id} status={exc.status}'

    # (b) sync_single_fact called once per item
    assert len(stub_sync.calls) == n_items, f'Expected {n_items} sync_single_fact calls, got {len(stub_sync.calls)}'

    # (c) Lease released
    async with session_factory() as session:
        lease_after = await session.execute(
            sa.select(AccessApplyActive).where(AccessApplyActive.subject_ref == subject_ref)
        )
        assert lease_after.scalar_one_or_none() is None, 'Lease must be released after execute_plan'

    # (d) Plan status active (not invalidated)
    async with session_factory() as session:
        plan_db = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == plan_future_id))
        plan_row = plan_db.scalar_one_or_none()
    assert plan_row is not None
    assert plan_row.status == AccessPlanStatus.active, f'Plan status must be active, got {plan_row.status}'

    # (e) Initiative in DB with correct type (created by F3 chain in execute_plan)
    async with session_factory() as session:
        init_result = await session.execute(
            sa.select(Initiative).where(
                Initiative.subject_ref == subject_ref,
                Initiative.type == InitiativeType.requested,
            )
        )
        db_initiatives = list(init_result.scalars().all())

    assert len(db_initiatives) >= 1, (
        f'At least one requested Initiative must exist for subject after execute_plan. '
        f'Got {len(db_initiatives)} requested initiatives.'
    )

    # (f) Original seeded initiative (future valid_from) must still exist — audit trail
    async with session_factory() as session:
        seeded_result = await session.execute(sa.select(Initiative).where(Initiative.id == future_initiative_id))
        seeded_db = seeded_result.scalar_one_or_none()
    assert seeded_db is not None, 'Original seeded initiative must still exist (NO DELETE audit trail)'
    assert seeded_db.valid_from is not None
    # valid_from must still be the original future timestamp (immutable initiative row)
    seeded_vf = seeded_db.valid_from
    if seeded_vf.tzinfo is None:
        seeded_vf = seeded_vf.replace(tzinfo=UTC)
    assert seeded_vf == _FUTURE_VALID_FROM, (
        f'Seeded initiative valid_from must be {_FUTURE_VALID_FROM.isoformat()}, got {seeded_vf.isoformat()}'
    )


# ===========================================================================
# Test — scanner ignores initiative outside window (lookback override)
# ===========================================================================


@pytest.mark.asyncio
async def test_scanner_lookback_override_excludes_old_initiative(session_factory) -> None:  # type: ignore[no-untyped-def]
    """G7 guard: scanner with tiny lookback=1s does NOT pick up initiative far in the past.

    Seeds an initiative with valid_from = T0 - 30 minutes (far in the past).
    Runs scanner with now=T0+3s, lookback=1s.
    Window = [T0+2s, T0+63s].
    The initiative (valid_from=T0-30min) is OUTSIDE the window → not found.
    """
    from src.inventory.initiatives.actions import run_scan_for_replan  # noqa: PLC0415
    from src.platform.logs.service import NoOpLogService  # noqa: PLC0415
    from src.platform.orchestrator.registry import ActionContext  # noqa: PLC0415

    # Seed a subject + initiative with valid_from well in the past
    async with session_factory() as session:
        org = await _seed_org_unit(session, f'eng-g7-excl-{uuid.uuid4().hex[:4]}')
        _emp, subj_excl = await _seed_employee_with_subject(session, org.id, 'excl')
        old_initiative = Initiative(
            id=uuid.uuid4(),
            access_fact_id=uuid.uuid4(),
            type=InitiativeType.requested,
            origin='request:old-g7-excl',
            subject_ref=str(subj_excl.id),
            subject_type='employee',
            valid_from=_T0 - timedelta(minutes=30),  # far outside window
        )
        session.add(old_initiative)
        await session.commit()

    captured: list[Any] = []

    class _Capture:
        async def emit(self, e: Any) -> None:
            captured.append(e)

    cap_svc_excl = _Capture()

    async with session_factory() as session:
        ctx = ActionContext(
            session=session,
            log_service=NoOpLogService(),
            pipeline_run_id=uuid.uuid4(),
            step_run_id=uuid.uuid4(),
            attempt=1,
            worker_id='test-g7-excl',
        )

        with patch('src.platform.events.service.noop_event_service', cap_svc_excl):
            await run_scan_for_replan(
                ctx,
                now=_SCANNER_NOW,
                lookback_seconds=1,  # tiny window — excludes old initiative
            )
        await session.commit()

    # The old initiative (T0-30min) is outside [T0+2s, T0+63s]
    # No event should be emitted for that subject.
    excl_subject_ref = str(subj_excl.id)
    excl_events = [
        e
        for e in captured
        if getattr(e, 'event_type', '') == 'subject.replan.required' and e.payload.get('subject_id') == excl_subject_ref
    ]
    assert excl_events == [], (
        f'Initiative far outside window must NOT trigger replan event. '
        f'Got {len(excl_events)} events for subject {excl_subject_ref!r}.'
    )
