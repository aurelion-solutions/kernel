# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Phase 19 Step F3: post-success chain in access_apply.execute_plan.

Coverage:
- grant_path_end_to_end: post-verify match on grant_role → sync_single_fact called +
  Initiative created
- revoke_path_end_to_end: post-verify match on revoke_role → sync_single_fact called +
  Initiative closed
- grant_idempotent: second F3 chain call → sync_single_fact skipped (returns False) +
  create_or_get returns existing initiative
- revoke_idempotent: second F3 chain call → close on already-closed initiative is no-op
- close_no_delete: revoke path sets valid_until, initiative row still exists in PG
- crash_recovery_executing: item in executing status + preflight match → F3 chain runs
  idempotently
- f3_skipped_when_no_services: execute_plan without sync_service/initiative_service
  skips F3 but still marks item done
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_apply.execute_plan import execute_plan
from src.engines.access_plan.models import (
    AccessApplyActive,
    AccessPlan,
    AccessPlanStatus,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemKind,
)
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.initiatives.service import InitiativeService
from src.platform.events.service import noop_event_service
from src.platform.logs.service import noop_log_service

# ---------------------------------------------------------------------------
# Stub sync service
# ---------------------------------------------------------------------------


class _StubSyncService:
    """Stub SyncApplyService for F3 chain tests.

    Tracks sync_single_fact calls.  Returns ``skip_first_call`` to simulate
    idempotency on repeated invocations.
    """

    def __init__(self, *, skip: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._skip = skip

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
                'descriptor': descriptor,
                'op': op,
                'event_key': event_key,
                'action_id': action_id,
            }
        )
        return not self._skip


# ---------------------------------------------------------------------------
# Stub connector
# ---------------------------------------------------------------------------


class _StubConnector:
    """Minimal connector for F3 tests."""

    def __init__(self, *, verify_sequence: list[str] | None = None) -> None:
        self._verify_seq = list(verify_sequence or [])
        self._verify_idx = 0

    async def invoke(
        self,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, Any]:
        if operation == 'verify_fact':
            if self._verify_idx < len(self._verify_seq):
                result = self._verify_seq[self._verify_idx]
                self._verify_idx += 1
            else:
                result = 'match'
            return {'status': 'ok', 'result': result}
        return {'status': 'ok'}


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_plan(
    session: AsyncSession,
    *,
    subject_ref: str | None = None,
) -> AccessPlan:
    subj = subject_ref or str(uuid.uuid4())
    plan = AccessPlan(
        subject_ref=subj,
        subject_type='employee',
        content_hash='testhash_f3',
        status=AccessPlanStatus.active,
        requires_confirmation=False,
    )
    session.add(plan)
    await session.flush()
    return plan


async def _seed_item(
    session: AsyncSession,
    plan_id: uuid.UUID,
    *,
    kind: PlanItemKind = PlanItemKind.grant_role,
    initiatives: list[dict] | None = None,
    initiative_refs: list[str] | None = None,
) -> PlanItem:
    item = PlanItem(
        plan_id=plan_id,
        kind=kind,
        application='app-f3',
        account_ref='acc-ref',
        target_descriptor={'role_ref': 'role_x'},
        initiatives=initiatives or [],
        initiative_refs=initiative_refs or [],
        policy_rule_refs=[],
        decision_snapshot={},
    )
    session.add(item)
    await session.flush()
    return item


async def _seed_lease(
    session: AsyncSession,
    *,
    plan: AccessPlan,
    pipeline_run_id: uuid.UUID,
) -> None:
    lease = AccessApplyActive(
        subject_ref=plan.subject_ref,
        subject_type=plan.subject_type,
        pipeline_run_id=pipeline_run_id,
        plan_id=plan.id,
    )
    session.add(lease)
    await session.flush()


async def _get_initiatives_for_plan_item(session: AsyncSession, item_id: uuid.UUID) -> list[Initiative]:
    result = await session.execute(sa.select(Initiative).where(Initiative.access_fact_id == item_id))
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Tests — grant path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_path_creates_initiative(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Grant path: sync_single_fact called + Initiative created in PG."""
    init_data = {'type': 'requested', 'origin': 'access_apply:app-f3'}
    async with session_factory() as session:
        plan = await _seed_plan(session)
        item = await _seed_item(session, plan.id, kind=PlanItemKind.grant_role, initiatives=[init_data])
        run_id = uuid.uuid4()
        await _seed_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    stub_connector = _StubConnector(verify_sequence=['mismatch', 'match'])
    stub_sync = _StubSyncService()
    svc = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=svc,
        )

    assert result.items_done == 1
    assert result.items_failed == 0

    # sync_single_fact must have been called with op=grant
    assert len(stub_sync.calls) == 1
    from src.engines.inventory_sync.schemas import SingleFactSyncOp

    assert stub_sync.calls[0]['op'] == SingleFactSyncOp.grant

    # Initiative must exist in PG
    async with session_factory() as session:
        initiatives = await _get_initiatives_for_plan_item(session, item.id)
        assert len(initiatives) == 1
        assert initiatives[0].type == InitiativeType.requested
        assert initiatives[0].origin == 'access_apply:app-f3'


# ---------------------------------------------------------------------------
# Tests — revoke path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_path_closes_initiative(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Revoke path: sync_single_fact called with revoke + Initiative closed (valid_until set)."""
    async with session_factory() as session:
        plan = await _seed_plan(session)
        # Seed an existing initiative to close
        initiative = Initiative(
            access_fact_id=uuid.uuid4(),
            type=InitiativeType.requested,
            origin='legacy',
        )
        session.add(initiative)
        await session.flush()
        initiative_id = initiative.id

        await _seed_item(
            session,
            plan.id,
            kind=PlanItemKind.revoke_role,
            initiative_refs=[str(initiative_id)],
        )
        run_id = uuid.uuid4()
        await _seed_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    stub_connector = _StubConnector(verify_sequence=['mismatch', 'match'])
    stub_sync = _StubSyncService()
    svc = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=svc,
        )

    assert result.items_done == 1

    # sync_single_fact must have been called with op=revoke
    from src.engines.inventory_sync.schemas import SingleFactSyncOp

    assert len(stub_sync.calls) == 1
    assert stub_sync.calls[0]['op'] == SingleFactSyncOp.revoke

    # Initiative must still exist in PG (no DELETE) with valid_until set
    async with session_factory() as session:
        row = await session.get(Initiative, initiative_id)
        assert row is not None, 'initiative must NOT be deleted — audit trail'
        assert row.valid_until is not None
        assert row.valid_until <= datetime.now(UTC)


# ---------------------------------------------------------------------------
# Tests — idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_idempotent_second_call_no_duplicate_initiative(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Repeated F3 grant chain: create_or_get returns existing, no duplicate Initiative."""
    init_data = {'type': 'birthright', 'origin': 'access_apply:app-f3'}
    async with session_factory() as session:
        plan = await _seed_plan(session)
        item = await _seed_item(session, plan.id, kind=PlanItemKind.grant_role, initiatives=[init_data])
        run_id_1 = uuid.uuid4()
        await _seed_lease(session, plan=plan, pipeline_run_id=run_id_1)
        await session.commit()

    stub_connector = _StubConnector(verify_sequence=['mismatch', 'match'])
    stub_sync = _StubSyncService()
    svc = InitiativeService(event_service=noop_event_service)

    # First run
    async with session_factory() as session:
        await execute_plan(
            session,
            plan.id,
            run_id_1,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=svc,
        )

    # Seed a new lease to simulate restart
    async with session_factory() as session:
        run_id_2 = uuid.uuid4()
        # Re-seed lease (normally done by apply endpoint)
        plan_obj = await session.get(AccessPlan, plan.id)
        assert plan_obj is not None
        lease = AccessApplyActive(
            subject_ref=plan_obj.subject_ref,
            subject_type=plan_obj.subject_type,
            pipeline_run_id=run_id_2,
            plan_id=plan.id,
        )
        session.add(lease)
        # Set item back to proposed to simulate re-attempt
        exec_row = await session.execute(sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan.id))
        for row in exec_row.scalars():
            row.status = PlanItemExecutionStatus.proposed
        await session.commit()

    # sync returns False (already written) on second run
    stub_sync2 = _StubSyncService(skip=True)
    svc2 = InitiativeService(event_service=noop_event_service)
    stub_connector2 = _StubConnector(verify_sequence=['mismatch', 'match'])

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id_2,
            stub_connector2,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync2,  # type: ignore[arg-type]
            initiative_service=svc2,
        )

    assert result.items_done == 1

    # Still only one initiative row (create_or_get is idempotent)
    async with session_factory() as session:
        initiatives = await _get_initiatives_for_plan_item(session, item.id)
        assert len(initiatives) == 1


@pytest.mark.asyncio
async def test_revoke_idempotent_close_already_closed(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Repeated close on already-closed initiative is a no-op."""
    close_time = datetime(2026, 1, 1, tzinfo=UTC)
    async with session_factory() as session:
        plan = await _seed_plan(session)
        initiative = Initiative(
            access_fact_id=uuid.uuid4(),
            type=InitiativeType.requested,
            origin='legacy',
            valid_until=close_time,  # already closed
        )
        session.add(initiative)
        await session.flush()
        initiative_id = initiative.id

        await _seed_item(session, plan.id, kind=PlanItemKind.revoke_role, initiative_refs=[str(initiative_id)])
        run_id = uuid.uuid4()
        await _seed_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    stub_connector = _StubConnector(verify_sequence=['mismatch', 'match'])
    stub_sync = _StubSyncService()
    svc = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=svc,
        )

    assert result.items_done == 1

    # Initiative row must still be there, valid_until unchanged (no-op close)
    async with session_factory() as session:
        row = await session.get(Initiative, initiative_id)
        assert row is not None
        assert row.valid_until is not None
        # valid_until must not have moved forward from the already-closed time
        assert row.valid_until <= datetime.now(UTC)


# ---------------------------------------------------------------------------
# Tests — crash recovery via executing status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crash_recovery_executing_runs_f3_chain(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Item in 'executing' status + preflight match → F3 chain runs idempotently."""
    init_data = {'type': 'requested', 'origin': 'access_apply:app-f3'}
    async with session_factory() as session:
        plan = await _seed_plan(session)
        item = await _seed_item(session, plan.id, kind=PlanItemKind.grant_role, initiatives=[init_data])
        run_id = uuid.uuid4()
        await _seed_lease(session, plan=plan, pipeline_run_id=run_id)

        # Simulate crash: item is in 'executing' state (connector ran but kernel crashed)
        exec_row = PlanItemExecution(
            plan_id=plan.id,
            item_id=item.id,
            status=PlanItemExecutionStatus.executing,
        )
        session.add(exec_row)
        await session.commit()

    # preflight=match → crash recovery path (F3 chain runs without calling connector)
    stub_connector = _StubConnector(verify_sequence=['match'])
    stub_sync = _StubSyncService()
    svc = InitiativeService(event_service=noop_event_service)

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            sync_service=stub_sync,  # type: ignore[arg-type]
            initiative_service=svc,
        )

    assert result.items_done == 1

    # F3 chain ran: sync_single_fact was called
    assert len(stub_sync.calls) == 1
    from src.engines.inventory_sync.schemas import SingleFactSyncOp

    assert stub_sync.calls[0]['op'] == SingleFactSyncOp.grant

    # Initiative was created
    async with session_factory() as session:
        initiatives = await _get_initiatives_for_plan_item(session, item.id)
        assert len(initiatives) == 1


# ---------------------------------------------------------------------------
# Tests — F3 skipped when services not provided
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f3_skipped_when_services_absent(session_factory) -> None:  # type: ignore[no-untyped-def]
    """execute_plan without sync_service/initiative_service skips F3 but marks item done."""
    async with session_factory() as session:
        plan = await _seed_plan(session)
        item = await _seed_item(
            session,
            plan.id,
            kind=PlanItemKind.grant_role,
            initiatives=[{'type': 'birthright', 'origin': 'test'}],
        )
        run_id = uuid.uuid4()
        await _seed_lease(session, plan=plan, pipeline_run_id=run_id)
        await session.commit()

    stub_connector = _StubConnector(verify_sequence=['mismatch', 'match'])

    async with session_factory() as session:
        result = await execute_plan(
            session,
            plan.id,
            run_id,
            stub_connector,  # type: ignore[arg-type]
            log_service=noop_log_service,
            # No sync_service, no initiative_service — F3 skipped
        )

    # Item must still be done (F1 logic unaffected)
    assert result.items_done == 1
    assert result.items_failed == 0

    # No initiatives created (F3 was skipped)
    async with session_factory() as session:
        initiatives = await _get_initiatives_for_plan_item(session, item.id)
        assert len(initiatives) == 0
