# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for access_plan ORM models (Phase 19 D1).

Covers:
- AccessPlan creation and default status
- PlanItem creation with JSONB arrays
- PlanDependency composite PK
- PlanItemExecution mutable state
- AccessApplyActive subject-level lease (INSERT ... ON CONFLICT DO NOTHING)
- AccountStatus.invited new enum value
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.engines.access_plan.models import (
    AccessApplyActive,
    AccessPlan,
    AccessPlanStatus,
    PlanDependency,
    PlanInvalidationReason,
    PlanItem,
    PlanItemExecution,
    PlanItemExecutionStatus,
    PlanItemFailureReason,
    PlanItemKind,
)
from src.inventory.accounts.models import AccountStatus

# ---------------------------------------------------------------------------
# Enum value checks (no DB required)
# ---------------------------------------------------------------------------


def test_access_plan_status_values() -> None:
    assert set(AccessPlanStatus) == {'active', 'superseded', 'cancelled', 'invalid'}


def test_plan_item_kind_covers_all_operations() -> None:
    expected = {
        'account_create',
        'account_invite',
        'account_activate',
        'account_suspend',
        'account_disable',
        'grant_role',
        'revoke_role',
        'group_add',
        'group_remove',
        'entitlement_attach',
        'entitlement_detach',
    }
    assert set(PlanItemKind) == expected


def test_plan_item_execution_status_values() -> None:
    assert set(PlanItemExecutionStatus) == {'proposed', 'executing', 'done', 'failed'}


def test_plan_item_failure_reason_values() -> None:
    assert set(PlanItemFailureReason) == {
        'precondition',
        'apply_error',
        'verify_mismatch',
        'verify_timeout',
    }


def test_account_status_has_invited() -> None:
    """Phase 19 D1 adds 'invited' to the existing AccountStatus enum."""
    assert AccountStatus.invited == 'invited'
    assert 'invited' in set(AccountStatus)


# ---------------------------------------------------------------------------
# Database integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_plan_creation_defaults(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AccessPlan persists with default status=active and no optional fields."""
    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref='emp-001',
            subject_type='employee',
            content_hash='a' * 64,
        )
        session.add(plan)
        await session.commit()
        plan_id = plan.id

    async with session_factory() as session:
        loaded = await session.get(AccessPlan, plan_id)
        assert loaded is not None
        assert loaded.status == AccessPlanStatus.active
        assert loaded.requires_confirmation is False
        assert loaded.idempotency_key is None
        assert loaded.supersedes_plan_id is None
        assert loaded.invalidation_reason is None
        assert loaded.invalidated_by_plan_id is None
        assert loaded.created_at is not None


@pytest.mark.asyncio
async def test_access_plan_supersedes_chain(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AccessPlan.supersedes_plan_id FK self-reference works correctly."""
    async with session_factory() as session:
        plan_a = AccessPlan(
            subject_ref='emp-002',
            subject_type='employee',
            content_hash='b' * 64,
        )
        session.add(plan_a)
        await session.flush()

        plan_b = AccessPlan(
            subject_ref='emp-002',
            subject_type='employee',
            content_hash='c' * 64,
            supersedes_plan_id=plan_a.id,
            status=AccessPlanStatus.active,
        )
        plan_a.status = AccessPlanStatus.superseded
        session.add(plan_b)
        await session.commit()

        plan_a_id = plan_a.id
        plan_b_id = plan_b.id

    async with session_factory() as session:
        b = await session.get(AccessPlan, plan_b_id)
        assert b is not None
        assert b.supersedes_plan_id == plan_a_id

        a = await session.get(AccessPlan, plan_a_id)
        assert a is not None
        assert a.status == AccessPlanStatus.superseded


@pytest.mark.asyncio
async def test_access_plan_invalidation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AccessPlan auto-invalidation columns persist correctly."""
    async with session_factory() as session:
        plan_a = AccessPlan(
            subject_ref='emp-003',
            subject_type='employee',
            content_hash='d' * 64,
        )
        session.add(plan_a)
        await session.flush()
        a_id = plan_a.id

        plan_b = AccessPlan(
            subject_ref='emp-003',
            subject_type='employee',
            content_hash='e' * 64,
        )
        session.add(plan_b)
        await session.flush()

        # Simulate auto-invalidation
        plan_a.status = AccessPlanStatus.invalid
        plan_a.invalidation_reason = PlanInvalidationReason.stale_after_apply
        plan_a.invalidated_by_plan_id = plan_b.id
        await session.commit()

    async with session_factory() as session:
        a = await session.get(AccessPlan, a_id)
        assert a is not None
        assert a.status == AccessPlanStatus.invalid
        assert a.invalidation_reason == PlanInvalidationReason.stale_after_apply
        assert a.invalidated_by_plan_id is not None


@pytest.mark.asyncio
async def test_access_plan_idempotency_key_unique(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unique partial index on idempotency_key rejects duplicates."""
    idem_key = f'idem-{uuid.uuid4()}'
    async with session_factory() as session:
        plan_a = AccessPlan(
            subject_ref='emp-004',
            subject_type='employee',
            content_hash='f' * 64,
            idempotency_key=idem_key,
        )
        session.add(plan_a)
        await session.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            plan_b = AccessPlan(
                subject_ref='emp-005',
                subject_type='employee',
                content_hash='g' * 64,
                idempotency_key=idem_key,
            )
            session.add(plan_b)
            await session.commit()


@pytest.mark.asyncio
async def test_plan_item_creation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PlanItem persists with JSONB arrays and target_descriptor."""
    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref='emp-006',
            subject_type='employee',
            content_hash='h' * 64,
        )
        session.add(plan)
        await session.flush()

        item = PlanItem(
            plan_id=plan.id,
            kind=PlanItemKind.grant_role,
            application='my-app',
            target_descriptor={'role': 'viewer', 'scope': 'global'},
            initiatives=[{'type': 'birthright', 'origin': 'policy_rule:r1'}],
            initiative_refs=[],
            policy_rule_refs=['r1', 'r2'],
            decision_snapshot={'decision': 'allow'},
        )
        session.add(item)
        await session.commit()
        item_id = item.id
        plan_id = plan.id

    async with session_factory() as session:
        loaded = await session.get(PlanItem, item_id)
        assert loaded is not None
        assert loaded.plan_id == plan_id
        assert loaded.kind == PlanItemKind.grant_role
        assert loaded.application == 'my-app'
        assert loaded.target_descriptor == {'role': 'viewer', 'scope': 'global'}
        assert loaded.initiatives == [{'type': 'birthright', 'origin': 'policy_rule:r1'}]
        assert loaded.policy_rule_refs == ['r1', 'r2']
        assert loaded.decision_snapshot == {'decision': 'allow'}


@pytest.mark.asyncio
async def test_plan_dependency_composite_pk(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PlanDependency persists with (plan_id, item_id, requires_item_id) composite PK."""
    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref='emp-007',
            subject_type='employee',
            content_hash='i' * 64,
        )
        session.add(plan)
        await session.flush()

        item_a = PlanItem(
            plan_id=plan.id,
            kind=PlanItemKind.account_create,
            application='app-x',
            target_descriptor={},
        )
        item_b = PlanItem(
            plan_id=plan.id,
            kind=PlanItemKind.grant_role,
            application='app-x',
            target_descriptor={'role': 'admin'},
        )
        session.add_all([item_a, item_b])
        await session.flush()

        dep = PlanDependency(
            plan_id=plan.id,
            item_id=item_b.id,
            requires_item_id=item_a.id,
        )
        session.add(dep)
        await session.commit()
        dep_key = (plan.id, item_b.id, item_a.id)

    async with session_factory() as session:
        loaded = await session.get(PlanDependency, dep_key)
        assert loaded is not None
        assert loaded.plan_id == dep_key[0]
        assert loaded.item_id == dep_key[1]
        assert loaded.requires_item_id == dep_key[2]


@pytest.mark.asyncio
async def test_plan_item_execution_lifecycle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PlanItemExecution transitions through proposed → executing → done."""
    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref='emp-008',
            subject_type='employee',
            content_hash='j' * 64,
        )
        session.add(plan)
        await session.flush()

        item = PlanItem(
            plan_id=plan.id,
            kind=PlanItemKind.account_activate,
            application='app-y',
            target_descriptor={},
        )
        session.add(item)
        await session.flush()

        exe = PlanItemExecution(plan_id=plan.id, item_id=item.id)
        session.add(exe)
        await session.commit()

        plan_id = plan.id
        item_id = item.id

    async with session_factory() as session:
        exe = await session.get(PlanItemExecution, (plan_id, item_id))
        assert exe is not None
        assert exe.status == PlanItemExecutionStatus.proposed

        exe.status = PlanItemExecutionStatus.executing
        await session.commit()

    async with session_factory() as session:
        exe = await session.get(PlanItemExecution, (plan_id, item_id))
        assert exe is not None
        assert exe.status == PlanItemExecutionStatus.executing

        exe.status = PlanItemExecutionStatus.done
        await session.commit()

    async with session_factory() as session:
        exe = await session.get(PlanItemExecution, (plan_id, item_id))
        assert exe is not None
        assert exe.status == PlanItemExecutionStatus.done


@pytest.mark.asyncio
async def test_plan_item_execution_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """PlanItemExecution records failure reason and error message."""
    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref='emp-009',
            subject_type='employee',
            content_hash='k' * 64,
        )
        session.add(plan)
        await session.flush()

        item = PlanItem(
            plan_id=plan.id,
            kind=PlanItemKind.revoke_role,
            application='app-z',
            target_descriptor={},
        )
        session.add(item)
        await session.flush()

        exe = PlanItemExecution(
            plan_id=plan.id,
            item_id=item.id,
            status=PlanItemExecutionStatus.failed,
            failure_reason=PlanItemFailureReason.verify_mismatch,
            last_error='connector returned unexpected state',
        )
        session.add(exe)
        await session.commit()
        plan_id, item_id = plan.id, item.id

    async with session_factory() as session:
        exe = await session.get(PlanItemExecution, (plan_id, item_id))
        assert exe is not None
        assert exe.status == PlanItemExecutionStatus.failed
        assert exe.failure_reason == PlanItemFailureReason.verify_mismatch
        assert exe.last_error == 'connector returned unexpected state'


@pytest.mark.asyncio
async def test_access_apply_active_conflict_do_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """INSERT ... ON CONFLICT (subject_ref) DO NOTHING blocks a second row for same subject."""
    subject_ref = f'emp-{uuid.uuid4()}'
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()

    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref=subject_ref,
            subject_type='employee',
            content_hash='l' * 64,
        )
        session.add(plan)
        await session.commit()
        plan_id = plan.id

    async with session_factory() as session:
        # First insert — should succeed
        await session.execute(
            sa.text(
                'INSERT INTO access_apply_active '
                '(subject_ref, subject_type, pipeline_run_id, plan_id) '
                'VALUES (:sr, :st, :run, :pid) '
                'ON CONFLICT (subject_ref) DO NOTHING'
            ),
            {'sr': subject_ref, 'st': 'employee', 'run': run_id_a, 'pid': plan_id},
        )

        # Second insert for same subject — do nothing
        result = await session.execute(
            sa.text(
                'INSERT INTO access_apply_active '
                '(subject_ref, subject_type, pipeline_run_id, plan_id) '
                'VALUES (:sr, :st, :run, :pid) '
                'ON CONFLICT (subject_ref) DO NOTHING '
                'RETURNING pipeline_run_id'
            ),
            {'sr': subject_ref, 'st': 'employee', 'run': run_id_b, 'pid': plan_id},
        )
        returning = result.fetchone()
        # No row returned because conflict → do nothing
        assert returning is None
        await session.commit()

    async with session_factory() as session:
        # Verify only first row persisted
        row = await session.get(AccessApplyActive, subject_ref)
        assert row is not None
        assert row.pipeline_run_id == run_id_a


@pytest.mark.asyncio
async def test_access_apply_active_delete_on_done(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """AccessApplyActive row can be deleted (simulating execute_plan finally block)."""
    subject_ref = f'emp-{uuid.uuid4()}'
    run_id = uuid.uuid4()

    async with session_factory() as session:
        plan = AccessPlan(
            subject_ref=subject_ref,
            subject_type='nhi',
            content_hash='m' * 64,
        )
        session.add(plan)
        await session.flush()

        lease = AccessApplyActive(
            subject_ref=subject_ref,
            subject_type='nhi',
            pipeline_run_id=run_id,
            plan_id=plan.id,
        )
        session.add(lease)
        await session.commit()

    async with session_factory() as session:
        loaded = await session.get(AccessApplyActive, subject_ref)
        assert loaded is not None
        await session.delete(loaded)
        await session.commit()

    async with session_factory() as session:
        gone = await session.get(AccessApplyActive, subject_ref)
        assert gone is None
