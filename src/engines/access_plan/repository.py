# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Repository helpers for the access_plan engine.

Responsibilities:
- Fetch current effective grants for a subject (from access_effective).
- Fetch current initiatives for a subject (from inventory.initiatives via
  JOIN access_facts).
- Fetch SubjectContext (employee or NHI attributes).
- DB queries for idempotency_key reuse and content_hash dedup.
- Auto-supersedes: mark older active plans superseded within the same TX.
- Count existing effective grants for a subject (safe-revoke threshold).
- Fetch account status per (application_id, subject_id) for D3 resolver.
- Fetch connector transitions descriptor for D3 resolver.

No business logic, no event emission.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_effective.models import EffectiveGrant
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
from src.inventory.accounts.models import Account
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.nhi.models import NHI, NHIAttribute
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.connectors.models import ConnectorInstance
from src.platform.connectors.registration_schemas import AccountStatusTransitions, ConnectorCapabilityDescriptor

_CONTENT_HASH_DEDUP_WINDOW_SECONDS = 5


# ---------------------------------------------------------------------------
# SubjectContext fetch
# ---------------------------------------------------------------------------


async def fetch_employee_context_data(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return raw context data for an employee: org_unit_id + attributes dict.

    Returns None if the employee does not exist.
    """
    employee_result = await session.execute(sa.select(Employee).where(Employee.id == employee_id))
    employee = employee_result.scalar_one_or_none()
    if employee is None:
        return None

    attr_result = await session.execute(
        sa.select(EmployeeAttribute).where(EmployeeAttribute.employee_id == employee_id)
    )
    attributes = {row.key: row.value for row in attr_result.scalars().all()}

    return {
        'org_unit_id': str(employee.org_unit_id) if employee.org_unit_id else None,
        'attributes': attributes,
    }


async def fetch_nhi_context_data(
    session: AsyncSession,
    nhi_id: uuid.UUID,
) -> dict[str, Any] | None:
    """Return raw context data for an NHI: application_ref, owner_subject_ref,
    expires_at, attributes dict.

    Returns None if the NHI does not exist.
    """
    nhi_result = await session.execute(sa.select(NHI).where(NHI.id == nhi_id))
    nhi = nhi_result.scalar_one_or_none()
    if nhi is None:
        return None

    attr_result = await session.execute(sa.select(NHIAttribute).where(NHIAttribute.nhi_id == nhi_id))
    attributes = {row.key: row.value for row in attr_result.scalars().all()}

    # Resolve owner subject_ref from the owner_employee → subjects table
    owner_subject_ref: str | None = None
    if nhi.owner_employee_id is not None:
        subject_result = await session.execute(
            sa.select(Subject).where(
                Subject.kind == SubjectKind.employee,
                Subject.principal_employee_id == nhi.owner_employee_id,
            )
        )
        subject = subject_result.scalar_one_or_none()
        if subject is not None:
            owner_subject_ref = str(subject.id)

    # application_ref: use application_id as string reference
    application_ref = str(nhi.application_id) if nhi.application_id else None

    # expires_at: read from attributes if set (key: 'expires_at')
    expires_at_str = attributes.pop('expires_at', None)
    expires_at: datetime | None = None
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
        except ValueError:
            pass

    return {
        'application_ref': application_ref,
        'owner_subject_ref': owner_subject_ref,
        'expires_at': expires_at,
        'attributes': attributes,
    }


# ---------------------------------------------------------------------------
# Subject kind resolution
# ---------------------------------------------------------------------------


async def resolve_subject_kind(
    session: AsyncSession,
    subject_ref: str,
) -> SubjectKind | None:
    """Resolve SubjectKind by looking up the subjects table by subject_ref (str UUID).

    Returns None if the subject row does not exist.
    """
    try:
        subject_uuid = uuid.UUID(subject_ref)
    except ValueError:
        return None

    result = await session.execute(sa.select(Subject).where(Subject.id == subject_uuid))
    subject = result.scalar_one_or_none()
    if subject is None:
        return None
    return SubjectKind(subject.kind)


async def fetch_subject_principal_ids(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Return (principal_employee_id, principal_nhi_id) for the given Subject.id.

    Returns (None, None) when the subject does not exist.
    Used by access_plan service to resolve the correct principal UUID before
    calling fetch_employee_context_data / fetch_nhi_context_data.
    """
    result = await session.execute(sa.select(Subject).where(Subject.id == subject_id))
    subject = result.scalar_one_or_none()
    if subject is None:
        return None, None
    return subject.principal_employee_id, subject.principal_nhi_id


async def resolve_subject_ref_for_nhi(
    session: AsyncSession,
    nhi_id: uuid.UUID,
) -> str | None:
    """Return the Subject.id (as str) whose principal_nhi_id == nhi_id.

    Used by fanout_replan_for_application to convert NHI.id to Subject.id
    before calling AccessPlanService.create_plan (which expects Subject.id).
    Returns None when no Subject row references this NHI.
    """
    result = await session.execute(
        sa.select(Subject).where(
            Subject.kind == SubjectKind.nhi,
            Subject.principal_nhi_id == nhi_id,
        )
    )
    subject = result.scalar_one_or_none()
    if subject is None:
        return None
    return str(subject.id)


# ---------------------------------------------------------------------------
# Current facts from access_effective
# ---------------------------------------------------------------------------


async def fetch_current_effective_grants(
    session: AsyncSession,
    subject_id: uuid.UUID,
    now: datetime,
) -> list[EffectiveGrant]:
    """Return non-tombstoned, non-expired effective grants for a subject."""
    stmt = (
        sa.select(EffectiveGrant)
        .where(
            EffectiveGrant.subject_id == subject_id,
            EffectiveGrant.tombstoned_at.is_(None),
            sa.or_(
                EffectiveGrant.valid_until.is_(None),
                EffectiveGrant.valid_until > now,
            ),
        )
        .order_by(EffectiveGrant.application_id, EffectiveGrant.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def count_current_effective_grants(
    session: AsyncSession,
    subject_id: uuid.UUID,
    now: datetime,
) -> int:
    """Count non-tombstoned, non-expired effective grants for safe-revoke threshold check."""
    stmt = sa.select(sa.func.count()).where(
        EffectiveGrant.subject_id == subject_id,
        EffectiveGrant.tombstoned_at.is_(None),
        sa.or_(
            EffectiveGrant.valid_until.is_(None),
            EffectiveGrant.valid_until > now,
        ),
    )
    result = await session.execute(stmt)
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Current initiatives from inventory
# ---------------------------------------------------------------------------


async def fetch_current_initiatives_for_subject(
    session: AsyncSession,
    subject_id: uuid.UUID,
    now: datetime,
) -> list[Initiative]:
    """Return active, non-expired initiatives for all access_facts owned by subject.

    Two query paths are merged and deduplicated:
    1. Legacy JOIN path: initiatives → access_facts (PG shim) → subjects.
       Works when access_facts PG shim rows exist.
    2. Direct subject_ref path: initiatives.subject_ref = :subject_id_str.
       Works for initiatives created by access_apply F3 chain (Phase 19+)
       which sets subject_ref directly (Initiative.subject_ref denormalised
       column added in Step E4).

    Results are deduplicated by initiative id so both paths can be safely
    combined without double-counting.
    """
    subject_id_str = str(subject_id)

    # Path 1: legacy JOIN with access_facts shim table
    legacy_stmt = sa.text(
        """
        SELECT i.id, i.access_fact_id, i.type, i.origin,
               i.valid_from, i.valid_until, i.created_at, i.updated_at
        FROM initiatives i
        JOIN access_facts af ON af.id = i.access_fact_id
        JOIN subjects s ON s.id = af.subject_id
        WHERE s.id = :subject_id
          AND (i.valid_until IS NULL OR i.valid_until > :now)
        """
    )

    # Path 2: direct subject_ref lookup (Phase 19 F3-chain created initiatives)
    direct_stmt = sa.text(
        """
        SELECT i.id, i.access_fact_id, i.type, i.origin,
               i.valid_from, i.valid_until, i.created_at, i.updated_at
        FROM initiatives i
        WHERE i.subject_ref = :subject_id_str
          AND (i.valid_until IS NULL OR i.valid_until > :now)
        """
    )

    seen: set[uuid.UUID] = set()
    initiatives: list[Initiative] = []

    for stmt, params in [
        (legacy_stmt, {'subject_id': subject_id, 'now': now}),
        (direct_stmt, {'subject_id_str': subject_id_str, 'now': now}),
    ]:
        result = await session.execute(stmt, params)
        for row in result.all():
            if row.id in seen:
                continue
            seen.add(row.id)
            init = Initiative(
                id=row.id,
                access_fact_id=row.access_fact_id,
                type=InitiativeType(row.type),
                origin=row.origin,
                valid_from=row.valid_from,
                valid_until=row.valid_until,
            )
            initiatives.append(init)

    initiatives.sort(key=lambda i: i.id)
    return initiatives


# ---------------------------------------------------------------------------
# Idempotency and content_hash dedup
# ---------------------------------------------------------------------------


async def find_plan_by_idempotency_key(
    session: AsyncSession,
    idempotency_key: str,
) -> AccessPlan | None:
    """Return an existing plan with the given idempotency_key (any status)."""
    result = await session.execute(sa.select(AccessPlan).where(AccessPlan.idempotency_key == idempotency_key))
    return result.scalar_one_or_none()


async def find_recent_active_plan_by_content_hash(
    session: AsyncSession,
    subject_ref: str,
    content_hash: str,
    now: datetime,
    window_seconds: int = _CONTENT_HASH_DEDUP_WINDOW_SECONDS,
) -> AccessPlan | None:
    """Return an active plan for the subject with matching content_hash created within
    the dedup window.  Returns None if no such plan exists.
    """
    cutoff = now - timedelta(seconds=window_seconds)
    result = await session.execute(
        sa.select(AccessPlan).where(
            AccessPlan.subject_ref == subject_ref,
            AccessPlan.status == AccessPlanStatus.active,
            AccessPlan.content_hash == content_hash,
            AccessPlan.created_at >= cutoff,
        )
    )
    return result.scalar_one_or_none()


async def find_active_plan_for_subject(
    session: AsyncSession,
    subject_ref: str,
) -> AccessPlan | None:
    """Return the most recently created active plan for the subject.

    Used for building the supersedes chain.
    """
    result = await session.execute(
        sa.select(AccessPlan)
        .where(
            AccessPlan.subject_ref == subject_ref,
            AccessPlan.status == AccessPlanStatus.active,
        )
        .order_by(AccessPlan.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Auto-supersedes
# ---------------------------------------------------------------------------


async def supersede_older_active_plans(
    session: AsyncSession,
    subject_ref: str,
    new_plan_id: uuid.UUID,
    now: datetime,
) -> int:
    """Mark all active plans for subject (excluding new_plan_id) as superseded.

    Returns the count of plans superseded.
    """
    stmt = (
        sa.update(AccessPlan)
        .where(
            AccessPlan.subject_ref == subject_ref,
            AccessPlan.status == AccessPlanStatus.active,
            AccessPlan.id != new_plan_id,
        )
        .values(status=AccessPlanStatus.superseded)
        .returning(AccessPlan.id)
    )
    result = await session.execute(stmt)
    return len(result.all())


# ---------------------------------------------------------------------------
# Plan item persistence
# ---------------------------------------------------------------------------


async def insert_plan_items(
    session: AsyncSession,
    items: list[PlanItem],
) -> None:
    """Bulk-insert plan items (append-only; no conflict handling)."""
    for item in items:
        session.add(item)
    await session.flush()


# ---------------------------------------------------------------------------
# D4 — plan dependency persistence
# ---------------------------------------------------------------------------


async def insert_plan_dependencies(
    session: AsyncSession,
    deps: list[PlanDependency],
) -> None:
    """Bulk-insert PlanDependency rows (append-only; no conflict handling)."""
    for dep in deps:
        session.add(dep)
    await session.flush()


# ---------------------------------------------------------------------------
# D4 — connector descriptor lookup
# ---------------------------------------------------------------------------


async def fetch_connector_descriptor(
    session: AsyncSession,
    instance_id: str,
) -> ConnectorCapabilityDescriptor | None:
    """Return the full ConnectorCapabilityDescriptor for a connector instance.

    Returns None when no row exists.  Raises on descriptor parse failure.
    """
    result = await session.execute(
        sa.select(ConnectorInstance.descriptor).where(ConnectorInstance.instance_id == instance_id)
    )
    raw = result.scalar_one_or_none()
    if raw is None:
        return None
    try:
        return ConnectorCapabilityDescriptor.model_validate(raw)
    except Exception:  # noqa: BLE001 # allowed-broad: provider boundary
        return None


# ---------------------------------------------------------------------------
# D3 — account status lookup
# ---------------------------------------------------------------------------


async def fetch_account_status_for_subject(
    session: AsyncSession,
    application_id: uuid.UUID,
    subject_id: uuid.UUID,
) -> str | None:
    """Return the account status string for (application_id, subject_id).

    Returns None when no account row exists (caller interprets as 'not_exists').
    """
    result = await session.execute(
        sa.select(Account.status).where(
            Account.application_id == application_id,
            Account.subject_id == subject_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    # AccountStatus is a StrEnum — return its string value
    return str(row)


# ---------------------------------------------------------------------------
# D3 — connector transitions lookup
# ---------------------------------------------------------------------------


async def fetch_connector_transitions(
    session: AsyncSession,
    instance_id: str,
) -> AccountStatusTransitions:
    """Return AccountStatusTransitions for the connector instance.

    Falls back to an empty (permissive-no-op) transitions object when the
    connector has no descriptor or the descriptor has no account_status field.
    """
    result = await session.execute(
        sa.select(ConnectorInstance.descriptor).where(ConnectorInstance.instance_id == instance_id)
    )
    raw = result.scalar_one_or_none()
    if raw is None:
        return AccountStatusTransitions()
    try:
        descriptor = ConnectorCapabilityDescriptor.model_validate(raw)
        return descriptor.account_status
    except Exception:  # noqa: BLE001 # allowed-broad: provider boundary
        return AccountStatusTransitions()


# ---------------------------------------------------------------------------
# F1 — execute_plan helpers
# ---------------------------------------------------------------------------


async def fetch_plan_by_id(
    session: AsyncSession,
    plan_id: uuid.UUID,
) -> AccessPlan | None:
    """Return AccessPlan by id, or None when not found."""
    result = await session.execute(sa.select(AccessPlan).where(AccessPlan.id == plan_id))
    return result.scalar_one_or_none()


async def fetch_plan_items_ordered(
    session: AsyncSession,
    plan_id: uuid.UUID,
) -> list[PlanItem]:
    """Return all PlanItems for a plan ordered by creation order (stable).

    Topological DAG order is applied by the executor after fetching.
    """
    result = await session.execute(sa.select(PlanItem).where(PlanItem.plan_id == plan_id).order_by(PlanItem.id))
    return list(result.scalars().all())


async def fetch_plan_deps(
    session: AsyncSession,
    plan_id: uuid.UUID,
) -> list[PlanDependency]:
    """Return all PlanDependency rows for a plan."""
    result = await session.execute(sa.select(PlanDependency).where(PlanDependency.plan_id == plan_id))
    return list(result.scalars().all())


async def fetch_item_executions(
    session: AsyncSession,
    plan_id: uuid.UUID,
) -> dict[uuid.UUID, PlanItemExecution]:
    """Return PlanItemExecution rows for a plan keyed by item_id."""
    result = await session.execute(sa.select(PlanItemExecution).where(PlanItemExecution.plan_id == plan_id))
    return {row.item_id: row for row in result.scalars().all()}


async def upsert_item_execution(
    session: AsyncSession,
    plan_id: uuid.UUID,
    item_id: uuid.UUID,
    status: PlanItemExecutionStatus,
    *,
    failure_reason: PlanItemFailureReason | None = None,
    last_error: str | None = None,
) -> None:
    """Insert or update a PlanItemExecution row.

    Uses INSERT ... ON CONFLICT (plan_id, item_id) DO UPDATE to handle both
    initial creation (proposed) and status transitions.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    now = datetime.now(UTC)
    stmt = pg_insert(PlanItemExecution).values(
        plan_id=plan_id,
        item_id=item_id,
        status=status,
        failure_reason=failure_reason,
        last_error=last_error,
        last_verified_at=now if status == PlanItemExecutionStatus.done else None,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=['plan_id', 'item_id'],
        set_={
            'status': stmt.excluded.status,
            'failure_reason': stmt.excluded.failure_reason,
            'last_error': stmt.excluded.last_error,
            'last_verified_at': stmt.excluded.last_verified_at,
        },
    )
    await session.execute(stmt)


async def invalidate_other_active_plans(
    session: AsyncSession,
    subject_ref: str,
    applying_plan_id: uuid.UUID,
) -> int:
    """Mark all other active plans for the subject as invalid (stale_after_apply).

    Returns the count of plans invalidated.
    """
    stmt = (
        sa.update(AccessPlan)
        .where(
            AccessPlan.subject_ref == subject_ref,
            AccessPlan.status == AccessPlanStatus.active,
            AccessPlan.id != applying_plan_id,
        )
        .values(
            status=AccessPlanStatus.invalid,
            invalidation_reason=PlanInvalidationReason.stale_after_apply,
            invalidated_by_plan_id=applying_plan_id,
        )
        .returning(AccessPlan.id)
    )
    result = await session.execute(stmt)
    return len(result.all())


async def delete_apply_lease(
    session: AsyncSession,
    pipeline_run_id: uuid.UUID,
) -> None:
    """Delete the subject-level apply lease row for the given pipeline run."""
    await session.execute(sa.delete(AccessApplyActive).where(AccessApplyActive.pipeline_run_id == pipeline_run_id))


# ---------------------------------------------------------------------------
# Cross-plan flat list of PlanItems with execution state
# ---------------------------------------------------------------------------


def _build_plan_items_cross_plan_stmt(
    *,
    execution_statuses: list[PlanItemExecutionStatus] | None,
    plan_status: str | None,
    kind: PlanItemKind | None,
    application: str | None,
    plan_id: uuid.UUID | None,
    subject_ref: str | None,
    subject_type: str | None,
) -> sa.Select:  # type: ignore[type-arg]
    """Build a SELECT for PlanItem JOIN AccessPlan JOIN PlanItemExecution with optional filters.

    Returns a SQLAlchemy select statement (without LIMIT/OFFSET).
    Columns: all PlanItem columns + plan_status, subject_ref, subject_type, plan.created_at,
             execution_status, failure_reason, last_verified_at, last_error.
    """
    stmt = (
        sa.select(
            PlanItem.id,
            PlanItem.plan_id,
            AccessPlan.status.label('plan_status'),
            AccessPlan.subject_ref,
            AccessPlan.subject_type,
            PlanItem.kind,
            PlanItem.application,
            PlanItem.account_ref,
            PlanItem.target_descriptor,
            PlanItem.initiatives,
            PlanItem.initiative_refs,
            PlanItem.policy_rule_refs,
            PlanItem.decision_snapshot,
            PlanItemExecution.status.label('execution_status'),
            PlanItemExecution.failure_reason,
            PlanItemExecution.last_verified_at,
            PlanItemExecution.last_error,
            AccessPlan.created_at,
        )
        .join(AccessPlan, AccessPlan.id == PlanItem.plan_id)
        .join(
            PlanItemExecution,
            sa.and_(
                PlanItemExecution.plan_id == PlanItem.plan_id,
                PlanItemExecution.item_id == PlanItem.id,
            ),
        )
        .order_by(AccessPlan.created_at.desc(), PlanItem.id)
    )

    if execution_statuses is not None:
        stmt = stmt.where(PlanItemExecution.status.in_(execution_statuses))
    if plan_status is not None:
        stmt = stmt.where(AccessPlan.status == plan_status)
    if kind is not None:
        stmt = stmt.where(PlanItem.kind == kind)
    if application is not None:
        stmt = stmt.where(PlanItem.application == application)
    if plan_id is not None:
        stmt = stmt.where(PlanItem.plan_id == plan_id)
    if subject_ref is not None:
        stmt = stmt.where(AccessPlan.subject_ref == subject_ref)
    if subject_type is not None:
        stmt = stmt.where(AccessPlan.subject_type == subject_type)

    return stmt


async def list_plan_items_cross_plan(
    session: AsyncSession,
    *,
    execution_statuses: list[PlanItemExecutionStatus] | None = None,
    plan_status: str | None = None,
    kind: PlanItemKind | None = None,
    application: str | None = None,
    plan_id: uuid.UUID | None = None,
    subject_ref: str | None = None,
    subject_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[sa.Row], int]:  # type: ignore[type-arg]
    """Return (rows, total) for flat plan items list across all plans.

    Each row has columns: id, plan_id, plan_status, subject_ref, subject_type,
    kind, application, account_ref, target_descriptor, initiatives, initiative_refs,
    policy_rule_refs, decision_snapshot, execution_status, failure_reason,
    last_verified_at, last_error, created_at.
    """
    base_stmt = _build_plan_items_cross_plan_stmt(
        execution_statuses=execution_statuses,
        plan_status=plan_status,
        kind=kind,
        application=application,
        plan_id=plan_id,
        subject_ref=subject_ref,
        subject_type=subject_type,
    )

    count_stmt = sa.select(sa.func.count()).select_from(base_stmt.subquery())
    total_result = await session.execute(count_stmt)
    total = total_result.scalar_one()

    data_stmt = base_stmt.limit(limit).offset(offset)
    data_result = await session.execute(data_stmt)
    rows = list(data_result.all())

    return rows, total


async def count_plan_items_cross_plan(
    session: AsyncSession,
    *,
    execution_statuses: list[PlanItemExecutionStatus] | None = None,
    plan_status: str | None = None,
    kind: PlanItemKind | None = None,
    application: str | None = None,
    plan_id: uuid.UUID | None = None,
    subject_ref: str | None = None,
    subject_type: str | None = None,
) -> int:
    """Return count of plan items matching the given filters."""
    base_stmt = _build_plan_items_cross_plan_stmt(
        execution_statuses=execution_statuses,
        plan_status=plan_status,
        kind=kind,
        application=application,
        plan_id=plan_id,
        subject_ref=subject_ref,
        subject_type=subject_type,
    )
    count_stmt = sa.select(sa.func.count()).select_from(base_stmt.subquery())
    result = await session.execute(count_stmt)
    return result.scalar_one()
