# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Master data apply — writes ReconciliationDeltaItem diffs to PG.

Reads ``pending`` delta items produced by ``master_data_pipeline`` and applies
them to the PG inventory tables:

  - ``person``   → INSERT / UPDATE persons
  - ``org_unit`` → INSERT / UPDATE org_units  (parent resolved via external_id)
  - ``employee`` → INSERT / UPDATE employees  (person + org_unit resolved via external_id)

Revoke operations are logged but NOT executed — master data does not support
soft-delete without an ``is_active`` column on the target model.  Items are
marked ``ignored`` so they do not block the run from completing.

Transaction discipline: every helper flushes but does NOT commit — the caller
(route or high-level entrypoint) owns the commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRunStatus,
)
from src.engines.inventory_reconcile.repository import (
    RunCounts,
    get_run,
    update_run_status,
)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MasterDataApplyResult:
    """Counts returned by each master data apply pass."""

    run_id: uuid.UUID
    entity_type: ReconciliationEntityType
    applied_count: int
    failed_count: int
    ignored_count: int


# ---------------------------------------------------------------------------
# Delta item loader
# ---------------------------------------------------------------------------


async def _load_pending_items(
    session: AsyncSession,
    run_id: uuid.UUID,
    entity_type: ReconciliationEntityType,
) -> list[ReconciliationDeltaItem]:
    stmt = (
        sa.select(ReconciliationDeltaItem)
        .where(ReconciliationDeltaItem.reconciliation_run_id == run_id)
        .where(ReconciliationDeltaItem.entity_type == entity_type)
        .where(ReconciliationDeltaItem.status == ReconciliationDeltaItemStatus.pending)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _mark_item(
    item: ReconciliationDeltaItem,
    status: ReconciliationDeltaItemStatus,
    *,
    entity_id: uuid.UUID | None = None,
    reason: str | None = None,
    session: AsyncSession,
) -> None:
    item.status = status
    item.applied_at = datetime.now(UTC)
    if entity_id is not None:
        item.entity_id = entity_id
    if reason is not None:
        item.reason = reason
    await session.flush()


# ---------------------------------------------------------------------------
# PERSONS apply
# ---------------------------------------------------------------------------


async def apply_persons_delta(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> MasterDataApplyResult:
    """Apply pending person delta items to the PG ``persons`` table."""
    from src.inventory.persons.models import Person  # noqa: PLC0415

    items = await _load_pending_items(session, run_id, ReconciliationEntityType.person)
    applied = failed = ignored = 0

    for item in items:
        try:
            if item.operation == ReconciliationDeltaOperation.create:
                data: dict[str, Any] = item.after_json or {}
                person = Person(
                    external_id=data['external_id'],
                    full_name=data['full_name'],
                )
                session.add(person)
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, entity_id=person.id, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                result = await session.execute(sa.select(Person).where(Person.id == item.entity_id))
                person = result.scalar_one()
                person.full_name = data['full_name']
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.revoke:
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.ignored,
                    reason='revoke not supported without is_active column',
                    session=session,
                )
                ignored += 1

            else:
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.ignored,
                    reason=f'unhandled operation: {item.operation}',
                    session=session,
                )
                ignored += 1

        except Exception as exc:  # noqa: BLE001 # allowed-broad: pipeline boundary
            await _mark_item(item, ReconciliationDeltaItemStatus.failed, reason=str(exc), session=session)
            failed += 1

    return MasterDataApplyResult(
        run_id=run_id,
        entity_type=ReconciliationEntityType.person,
        applied_count=applied,
        failed_count=failed,
        ignored_count=ignored,
    )


# ---------------------------------------------------------------------------
# ORG UNITS apply
# ---------------------------------------------------------------------------


async def apply_org_units_delta(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> MasterDataApplyResult:
    """Apply pending org_unit delta items to the PG ``org_units`` table.

    parent_external_id is resolved to parent_id at apply time.
    If the parent does not exist in PG yet the org unit is still created
    with ``parent_id=None`` (orphan); it can be re-reconciled once the parent
    is available.
    """
    from src.inventory.org_units.models import OrgUnit  # noqa: PLC0415

    items = await _load_pending_items(session, run_id, ReconciliationEntityType.org_unit)
    applied = failed = ignored = 0

    for item in items:
        try:
            if item.operation == ReconciliationDeltaOperation.create:
                data = item.after_json or {}
                parent_id = await _resolve_org_unit_id(session, data.get('parent_external_id'))
                ou = OrgUnit(
                    external_id=data['external_id'],
                    name=data['name'],
                    parent_id=parent_id,
                )
                session.add(ou)
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, entity_id=ou.id, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                result = await session.execute(sa.select(OrgUnit).where(OrgUnit.id == item.entity_id))
                ou = result.scalar_one()
                ou.name = data['name']
                ou.parent_id = await _resolve_org_unit_id(session, data.get('parent_external_id'))
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.revoke:
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.ignored,
                    reason='revoke not supported without is_active column',
                    session=session,
                )
                ignored += 1

            else:
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.ignored,
                    reason=f'unhandled operation: {item.operation}',
                    session=session,
                )
                ignored += 1

        except Exception as exc:  # noqa: BLE001 # allowed-broad: pipeline boundary
            await _mark_item(item, ReconciliationDeltaItemStatus.failed, reason=str(exc), session=session)
            failed += 1

    return MasterDataApplyResult(
        run_id=run_id,
        entity_type=ReconciliationEntityType.org_unit,
        applied_count=applied,
        failed_count=failed,
        ignored_count=ignored,
    )


async def _resolve_org_unit_id(session: AsyncSession, external_id: str | None) -> uuid.UUID | None:
    if not external_id:
        return None
    from src.inventory.org_units.models import OrgUnit  # noqa: PLC0415

    result = await session.execute(sa.select(OrgUnit.id).where(OrgUnit.external_id == external_id))
    row = result.scalar_one_or_none()
    return row


# ---------------------------------------------------------------------------
# EMPLOYEES apply
# ---------------------------------------------------------------------------


async def apply_employees_delta(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> MasterDataApplyResult:
    """Apply pending employee delta items to the PG ``employees`` table.

    person_external_id and org_unit_external_id are resolved to UUIDs at apply time.
    If the person does not exist the item is marked ``failed``.
    If the org_unit does not exist the employee is created with ``org_unit_id=None``.
    """
    from src.inventory.employees.models import Employee  # noqa: PLC0415

    items = await _load_pending_items(session, run_id, ReconciliationEntityType.employee)
    applied = failed = ignored = 0

    for item in items:
        try:
            if item.operation == ReconciliationDeltaOperation.create:
                data = item.after_json or {}
                person_id = await _resolve_person_id(session, data['person_external_id'])
                if person_id is None:
                    await _mark_item(
                        item,
                        ReconciliationDeltaItemStatus.failed,
                        reason=f'person not found: {data["person_external_id"]}',
                        session=session,
                    )
                    failed += 1
                    continue

                org_unit_id = await _resolve_org_unit_id(session, data.get('org_unit_external_id'))
                employee = Employee(
                    person_id=person_id,
                    is_locked=bool(data.get('is_locked', False)),
                    description=data.get('description'),
                    org_unit_id=org_unit_id,
                )
                session.add(employee)
                await session.flush()

                subject = await _ensure_subject_for_employee(session, employee.id)
                await _link_accounts_by_email(session, data.get('attributes'), subject.id)

                await _mark_item(item, ReconciliationDeltaItemStatus.applied, entity_id=employee.id, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                result = await session.execute(sa.select(Employee).where(Employee.id == item.entity_id))
                employee = result.scalar_one()
                employee.is_locked = bool(data.get('is_locked', False))
                employee.description = data.get('description')
                employee.org_unit_id = await _resolve_org_unit_id(session, data.get('org_unit_external_id'))
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.revoke:
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.ignored,
                    reason='revoke not supported without is_active column',
                    session=session,
                )
                ignored += 1

            else:
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.ignored,
                    reason=f'unhandled operation: {item.operation}',
                    session=session,
                )
                ignored += 1

        except Exception as exc:  # noqa: BLE001 # allowed-broad: pipeline boundary
            await _mark_item(item, ReconciliationDeltaItemStatus.failed, reason=str(exc), session=session)
            failed += 1

    return MasterDataApplyResult(
        run_id=run_id,
        entity_type=ReconciliationEntityType.employee,
        applied_count=applied,
        failed_count=failed,
        ignored_count=ignored,
    )


async def _ensure_subject_for_employee(session: AsyncSession, employee_id: uuid.UUID):
    """Return existing Subject for employee or create one."""
    from src.inventory.subjects.models import Subject, SubjectKind  # noqa: PLC0415

    result = await session.execute(sa.select(Subject).where(Subject.principal_employee_id == employee_id))
    subject = result.scalar_one_or_none()
    if subject is None:
        subject = Subject(
            external_id=str(uuid.uuid4()),
            kind=SubjectKind.employee,
            principal_employee_id=employee_id,
            status='active',
        )
        session.add(subject)
        await session.flush()
    return subject


async def _link_accounts_by_email(
    session: AsyncSession,
    attributes_raw: Any,
    subject_id: uuid.UUID,
) -> None:
    """Set subject_id on any Account whose username matches the employee email attribute."""
    from src.inventory.accounts.models import Account  # noqa: PLC0415

    if not attributes_raw:
        return

    if isinstance(attributes_raw, str):
        try:
            attributes = json.loads(attributes_raw)
        except (json.JSONDecodeError, ValueError):
            return
    elif isinstance(attributes_raw, dict):
        attributes = attributes_raw
    else:
        return

    email = attributes.get('email')
    if not email:
        return

    await session.execute(
        sa.update(Account)
        .where(Account.username == email)
        .where(Account.subject_id.is_(None))
        .values(subject_id=subject_id)
    )
    await session.flush()


async def _resolve_person_id(session: AsyncSession, external_id: str) -> uuid.UUID | None:
    from src.inventory.persons.models import Person  # noqa: PLC0415

    result = await session.execute(sa.select(Person.id).where(Person.external_id == external_id))
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# ACCOUNTS apply
# ---------------------------------------------------------------------------


async def apply_accounts_delta(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
) -> MasterDataApplyResult:
    """Apply pending account delta items to the PG ``ent_accounts`` table.

    Operations:
      create     — INSERT new Account row from after_json.
      update     — UPDATE existing Account row according to after_json diff.
      revoke     — UPDATE Account.status='disabled'.
      reactivate — UPDATE Account.status='active'.
      noop       — ignored.
    """
    from src.inventory.accounts.models import Account, AccountStatus  # noqa: PLC0415

    items = await _load_pending_items(session, run_id, ReconciliationEntityType.account)
    applied = failed = ignored = 0

    for item in items:
        try:
            if item.operation == ReconciliationDeltaOperation.create:
                data: dict[str, Any] = item.after_json or {}
                account = Account(
                    application_id=uuid.UUID(data['application_id']),
                    username=data['username'],
                    display_name=data.get('display_name'),
                    email=data.get('email'),
                    is_privileged=bool(data.get('is_privileged', False)),
                    mfa_enabled=bool(data.get('mfa_enabled', False)),
                    status=AccountStatus(data.get('status', AccountStatus.active)),
                    meta=data.get('meta', {}),
                )
                session.add(account)
                await session.flush()
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.applied,
                    entity_id=account.id,
                    session=session,
                )
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                result = await session.execute(sa.select(Account).where(Account.id == item.entity_id))
                account = result.scalar_one()
                if 'display_name' in data:
                    account.display_name = data['display_name']
                if 'email' in data:
                    account.email = data['email']
                if 'is_privileged' in data:
                    account.is_privileged = bool(data['is_privileged'])
                if 'mfa_enabled' in data:
                    account.mfa_enabled = bool(data['mfa_enabled'])
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.revoke:
                result = await session.execute(sa.select(Account).where(Account.id == item.entity_id))
                account = result.scalar_one()
                account.status = AccountStatus.disabled
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.reactivate:
                result = await session.execute(sa.select(Account).where(Account.id == item.entity_id))
                account = result.scalar_one()
                account.status = AccountStatus.active
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                applied += 1

            else:
                # noop
                await _mark_item(
                    item,
                    ReconciliationDeltaItemStatus.ignored,
                    reason=f'unhandled operation: {item.operation}',
                    session=session,
                )
                ignored += 1

        except Exception as exc:  # noqa: BLE001 # allowed-broad: pipeline boundary
            await _mark_item(item, ReconciliationDeltaItemStatus.failed, reason=str(exc), session=session)
            failed += 1

    return MasterDataApplyResult(
        run_id=run_id,
        entity_type=ReconciliationEntityType.account,
        applied_count=applied,
        failed_count=failed,
        ignored_count=ignored,
    )


# ---------------------------------------------------------------------------
# High-level entrypoint
# ---------------------------------------------------------------------------


async def apply_master_data_delta(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    entity_type: ReconciliationEntityType,
) -> MasterDataApplyResult:
    """Apply all pending delta items for one entity type and update the run status.

    Intended to be called after ``run_master_data_reconciliation(..., dry_run=False)``.
    The run must be in ``pending_apply`` status.

    Raises ``ValueError`` if entity_type is ``access_fact`` (wrong entrypoint).
    Raises ``LookupError`` if the run does not exist.
    """
    if entity_type == ReconciliationEntityType.access_fact:
        raise ValueError('Use SyncApplyService for access_fact entity type')

    run = await get_run(session, run_id)
    if run is None:
        raise LookupError(f'ReconciliationRun {run_id} not found')
    # Accept pending_apply (first call) or applied/partially_applied (fan-out: subsequent
    # entity_type call after a sibling already advanced the status). In the fan-out case
    # there are simply no pending items left for this entity_type — the result is a no-op.
    _ACCEPTABLE_STATUSES = {
        ReconciliationRunStatus.pending_apply,
        ReconciliationRunStatus.applied,
        ReconciliationRunStatus.partially_applied,
    }
    if run.status not in _ACCEPTABLE_STATUSES:
        raise ValueError(f'Run {run_id} is not in pending_apply status (got {run.status})')

    dispatch = {
        ReconciliationEntityType.person: apply_persons_delta,
        ReconciliationEntityType.org_unit: apply_org_units_delta,
        ReconciliationEntityType.employee: apply_employees_delta,
        ReconciliationEntityType.account: apply_accounts_delta,
    }
    apply_fn = dispatch[entity_type]

    try:
        result = await apply_fn(session, run_id=run_id)
    except Exception as exc:  # noqa: BLE001 # allowed-broad: pipeline boundary
        await update_run_status(session, run_id, status=ReconciliationRunStatus.failed, error=str(exc))
        raise

    if result.failed_count > 0 and result.applied_count == 0:
        final_status = ReconciliationRunStatus.failed
    elif result.failed_count > 0:
        final_status = ReconciliationRunStatus.partially_applied
    else:
        final_status = ReconciliationRunStatus.applied

    await update_run_status(
        session,
        run_id,
        status=final_status,
        counts=RunCounts(
            created=result.applied_count,
        ),
    )

    return result
