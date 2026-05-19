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

Event emission (Phase 20 K-B + K-G):
Every successful create/update apply emits one ``inventory.<entity>.created``
or ``inventory.<entity>.updated`` event with the unified payload shape from
K-A. Callers that already had per-row knowledge (e.g. the API route emitter)
keep working unchanged — these reconciliation-path events are additive and
travel the same routing keys. The ``event_service`` parameter is optional
and defaults to ``noop_event_service``; production wires the real service.
"""

from __future__ import annotations

from collections.abc import Mapping
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
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.service import SubjectService
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory_reconcile.master_data_apply'


# ---------------------------------------------------------------------------
# Event builders (Phase 20 K-B + K-G)
# ---------------------------------------------------------------------------


def _diff_changes(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    fields: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Return ``{field: {old, new}}`` for fields whose value differs.

    A field absent from either side is treated as ``None``.
    """
    before_d = before or {}
    after_d = after or {}
    changes: dict[str, dict[str, Any]] = {}
    for field in fields:
        old = before_d.get(field)
        new = after_d.get(field)
        if old != new:
            changes[field] = {'old': old, 'new': new}
    return changes


def _emit_created(
    events: EventService,
    *,
    event_type: str,
    entity_id: uuid.UUID,
    payload_extras: dict[str, Any],
) -> EventEnvelope:
    """Build a ``inventory.<entity>.created`` envelope. ``payload_extras`` carries
    the verbatim ``after_json`` fields (entity-shaped, not changes-shaped)."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        correlation_id=uuid.uuid4().hex,
        causation_id=None,
        payload={'entity_id': str(entity_id), **payload_extras},
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(entity_id),
    )


def _emit_updated(
    *,
    event_type: str,
    entity_id: uuid.UUID,
    changes: Mapping[str, Mapping[str, Any]],
    extras: dict[str, Any] | None = None,
) -> EventEnvelope:
    """Build a ``inventory.<entity>.updated`` envelope carrying ``changes: {field: {old, new}}``."""
    payload: dict[str, Any] = {'entity_id': str(entity_id), 'changes': dict(changes)}
    if extras:
        payload.update(extras)
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        correlation_id=uuid.uuid4().hex,
        causation_id=None,
        payload=payload,
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(entity_id),
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
    event_service: EventService | None = None,
) -> MasterDataApplyResult:
    """Apply pending person delta items to the PG ``persons`` table."""
    from src.inventory.persons.models import Person  # noqa: PLC0415

    events = event_service if event_service is not None else noop_event_service
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
                await events.emit(
                    _emit_created(
                        events,
                        event_type='inventory.person.created',
                        entity_id=person.id,
                        payload_extras={
                            'external_id': person.external_id,
                            'full_name': person.full_name,
                        },
                    )
                )
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                result = await session.execute(sa.select(Person).where(Person.id == item.entity_id))
                person = result.scalar_one()
                changes = _diff_changes(item.before_json, data, fields=('full_name',))
                person.full_name = data['full_name']
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                if changes:
                    await events.emit(
                        _emit_updated(
                            event_type='inventory.person.updated',
                            entity_id=person.id,
                            changes=changes,
                        )
                    )
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
    event_service: EventService | None = None,
) -> MasterDataApplyResult:
    """Apply pending org_unit delta items to the PG ``org_units`` table.

    parent_external_id is resolved to parent_id at apply time.
    If the parent does not exist in PG yet the org unit is still created
    with ``parent_id=None`` (orphan); it can be re-reconciled once the parent
    is available.

    Emits ``inventory.org_unit.created`` / ``inventory.org_unit.updated``
    events (Phase 20 K-G).
    """
    from src.inventory.org_units.models import OrgUnit  # noqa: PLC0415

    events = event_service if event_service is not None else noop_event_service
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
                await events.emit(
                    _emit_created(
                        events,
                        event_type='inventory.org_unit.created',
                        entity_id=ou.id,
                        payload_extras={
                            'external_id': ou.external_id,
                            'name': ou.name,
                            'parent_id': str(ou.parent_id) if ou.parent_id else None,
                        },
                    )
                )
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                result = await session.execute(sa.select(OrgUnit).where(OrgUnit.id == item.entity_id))
                ou = result.scalar_one()
                before_view = {
                    'name': ou.name,
                    'parent_external_id': item.before_json.get('parent_external_id') if item.before_json else None,
                }
                ou.name = data['name']
                ou.parent_id = await _resolve_org_unit_id(session, data.get('parent_external_id'))
                await session.flush()
                changes = _diff_changes(
                    before_view,
                    {'name': ou.name, 'parent_external_id': data.get('parent_external_id')},
                    fields=('name', 'parent_external_id'),
                )
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                if changes:
                    await events.emit(
                        _emit_updated(
                            event_type='inventory.org_unit.updated',
                            entity_id=ou.id,
                            changes=changes,
                        )
                    )
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
    event_service: EventService | None = None,
    subject_service: SubjectService | None = None,
) -> MasterDataApplyResult:
    """Apply pending employee delta items to the PG ``employees`` table.

    person_external_id and org_unit_external_id are resolved to UUIDs at apply time.
    If the person does not exist the item is marked ``failed``.
    If the org_unit does not exist the employee is created with ``org_unit_id=None``.

    Phase 20 K-B: ``attributes`` in the delta payload is now applied to
    ``ent_employee_attributes`` (formerly ignored). Any key under
    ``after_json["attributes"]`` is upserted; per-attribute deltas appear in
    the emitted ``inventory.employee.updated`` event under
    ``changes["attributes.<key>"]``. Emits ``inventory.employee.created`` on
    create and ``inventory.employee.updated`` on update.
    """
    from src.inventory.employees.models import Employee, EmployeeAttribute  # noqa: PLC0415

    events = event_service if event_service is not None else noop_event_service
    subjects = subject_service if subject_service is not None else SubjectService(event_service=events)
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

                # Apply attributes (generic — not just employment_status).
                attrs_raw = data.get('attributes')
                attrs_parsed = _parse_attributes(attrs_raw)
                for key, value in attrs_parsed.items():
                    session.add(EmployeeAttribute(employee_id=employee.id, key=key, value=value))
                if attrs_parsed:
                    await session.flush()

                subject = await subjects.ensure_for_principal(
                    session,
                    kind=SubjectKind.employee,
                    principal_id=employee.id,
                )
                await _link_accounts_by_email(session, attrs_raw, subject.id)

                await _mark_item(item, ReconciliationDeltaItemStatus.applied, entity_id=employee.id, session=session)
                await events.emit(
                    _emit_created(
                        events,
                        event_type='inventory.employee.created',
                        entity_id=employee.id,
                        payload_extras={
                            'subject_ref': str(subject.id),
                            'subject_type': 'employee',
                            'person_id': str(employee.person_id),
                            'org_unit_id': str(employee.org_unit_id) if employee.org_unit_id else None,
                            'is_locked': employee.is_locked,
                            'attributes': attrs_parsed,
                        },
                    )
                )
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                before = item.before_json or {}
                result = await session.execute(sa.select(Employee).where(Employee.id == item.entity_id))
                employee = result.scalar_one()

                # Build a synthetic before/after view that mirrors the planning attributes,
                # then apply attribute deltas at the row level too.
                before_view: dict[str, Any] = {
                    'is_locked': bool(before.get('is_locked', False)),
                    'description': before.get('description'),
                    'org_unit_external_id': before.get('org_unit_external_id'),
                }
                employee.is_locked = bool(data.get('is_locked', False))
                employee.description = data.get('description')
                employee.org_unit_id = await _resolve_org_unit_id(session, data.get('org_unit_external_id'))
                await session.flush()

                after_view: dict[str, Any] = {
                    'is_locked': employee.is_locked,
                    'description': employee.description,
                    'org_unit_external_id': data.get('org_unit_external_id'),
                }
                changes = _diff_changes(
                    before_view,
                    after_view,
                    fields=('is_locked', 'description', 'org_unit_external_id'),
                )

                # Attribute changes (Phase 20 K-B — generic, not only employment_status).
                attr_changes = await _apply_employee_attributes(
                    session,
                    employee_id=employee.id,
                    before_attrs_raw=before.get('attributes'),
                    after_attrs_raw=data.get('attributes'),
                )
                for key, change in attr_changes.items():
                    changes[f'attributes.{key}'] = change

                update_subject = await subjects.ensure_for_principal(
                    session,
                    kind=SubjectKind.employee,
                    principal_id=employee.id,
                )
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                if changes:
                    await events.emit(
                        _emit_updated(
                            event_type='inventory.employee.updated',
                            entity_id=employee.id,
                            changes=changes,
                            extras={
                                'subject_ref': str(update_subject.id),
                                'subject_type': 'employee',
                            },
                        )
                    )
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


def _parse_attributes(raw: Any) -> dict[str, str]:
    """Coerce the polymorphic ``attributes`` field on a delta item into a flat ``{key: str}`` dict.

    The reconciliation engine stores attributes either as a JSON-string
    (legacy CSV ingest path) or as a dict (master-data API path). Both
    flavours produce the same shape after parsing.
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
    elif isinstance(raw, dict):
        parsed = raw
    else:
        return {}
    return {str(k): str(v) for k, v in parsed.items() if v is not None}


async def _apply_employee_attributes(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    before_attrs_raw: Any,
    after_attrs_raw: Any,
) -> dict[str, dict[str, Any]]:
    """Upsert ``ent_employee_attributes`` for every key whose value differs.

    Returns ``{key: {old, new}}`` for change-emit.
    """
    from src.inventory.employees.models import EmployeeAttribute  # noqa: PLC0415

    before_attrs = _parse_attributes(before_attrs_raw)
    after_attrs = _parse_attributes(after_attrs_raw)

    all_keys = set(before_attrs) | set(after_attrs)
    changes: dict[str, dict[str, Any]] = {}

    for key in all_keys:
        old_value = before_attrs.get(key)
        new_value = after_attrs.get(key)
        if old_value == new_value:
            continue
        if new_value is None:
            # Currently we do not delete attributes from PG — record the change
            # for the event, but leave the row intact. Future work can adopt
            # an explicit attribute delete operation when needed.
            changes[key] = {'old': old_value, 'new': None}
            continue
        existing = await session.execute(
            sa.select(EmployeeAttribute)
            .where(EmployeeAttribute.employee_id == employee_id)
            .where(EmployeeAttribute.key == key)
        )
        existing_attr = existing.scalar_one_or_none()
        if existing_attr is None:
            session.add(EmployeeAttribute(employee_id=employee_id, key=key, value=new_value))
        else:
            existing_attr.value = new_value
        changes[key] = {'old': old_value, 'new': new_value}

    if changes:
        await session.flush()
    return changes


# ---------------------------------------------------------------------------
# ACCOUNTS apply
# ---------------------------------------------------------------------------


async def apply_accounts_delta(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    event_service: EventService | None = None,
) -> MasterDataApplyResult:
    """Apply pending account delta items to the PG ``ent_accounts`` table.

    Operations:
      create     — INSERT new Account row from after_json (emits ``inventory.account.created``).
      update     — UPDATE existing Account row according to after_json diff (emits ``inventory.account.updated``).
      revoke     — UPDATE Account.status='disabled' (emits ``inventory.account.updated`` with status change).
      reactivate — UPDATE Account.status='active' (emits ``inventory.account.updated`` with status change).
      noop       — ignored.
    """
    from src.inventory.accounts.models import Account, AccountStatus  # noqa: PLC0415

    events = event_service if event_service is not None else noop_event_service
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
                await events.emit(
                    _emit_created(
                        events,
                        event_type='inventory.account.created',
                        entity_id=account.id,
                        payload_extras={
                            'application_id': str(account.application_id),
                            'username': account.username,
                            'status': account.status.value if account.status is not None else None,
                        },
                    )
                )
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.update:
                data = item.after_json or {}
                before = item.before_json or {}
                result = await session.execute(sa.select(Account).where(Account.id == item.entity_id))
                account = result.scalar_one()
                before_view: dict[str, Any] = {}
                after_view: dict[str, Any] = {}
                fields: list[str] = []
                if 'display_name' in data:
                    before_view['display_name'] = before.get('display_name', account.display_name)
                    account.display_name = data['display_name']
                    after_view['display_name'] = account.display_name
                    fields.append('display_name')
                if 'email' in data:
                    before_view['email'] = before.get('email', account.email)
                    account.email = data['email']
                    after_view['email'] = account.email
                    fields.append('email')
                if 'is_privileged' in data:
                    before_view['is_privileged'] = before.get('is_privileged', account.is_privileged)
                    account.is_privileged = bool(data['is_privileged'])
                    after_view['is_privileged'] = account.is_privileged
                    fields.append('is_privileged')
                if 'mfa_enabled' in data:
                    before_view['mfa_enabled'] = before.get('mfa_enabled', account.mfa_enabled)
                    account.mfa_enabled = bool(data['mfa_enabled'])
                    after_view['mfa_enabled'] = account.mfa_enabled
                    fields.append('mfa_enabled')
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                changes = _diff_changes(before_view, after_view, fields=tuple(fields))
                if changes:
                    await events.emit(
                        _emit_updated(
                            event_type='inventory.account.updated',
                            entity_id=account.id,
                            changes=changes,
                        )
                    )
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.revoke:
                result = await session.execute(sa.select(Account).where(Account.id == item.entity_id))
                account = result.scalar_one()
                old_status = account.status.value if account.status is not None else None
                account.status = AccountStatus.disabled
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                await events.emit(
                    _emit_updated(
                        event_type='inventory.account.updated',
                        entity_id=account.id,
                        changes={'status': {'old': old_status, 'new': AccountStatus.disabled.value}},
                    )
                )
                applied += 1

            elif item.operation == ReconciliationDeltaOperation.reactivate:
                result = await session.execute(sa.select(Account).where(Account.id == item.entity_id))
                account = result.scalar_one()
                old_status = account.status.value if account.status is not None else None
                account.status = AccountStatus.active
                await session.flush()
                await _mark_item(item, ReconciliationDeltaItemStatus.applied, session=session)
                await events.emit(
                    _emit_updated(
                        event_type='inventory.account.updated',
                        entity_id=account.id,
                        changes={'status': {'old': old_status, 'new': AccountStatus.active.value}},
                    )
                )
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
    event_service: EventService | None = None,
    subject_service: SubjectService | None = None,
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
        if entity_type == ReconciliationEntityType.employee:
            result = await apply_fn(
                session,
                run_id=run_id,
                event_service=event_service,
                subject_service=subject_service,
            )
        else:
            result = await apply_fn(session, run_id=run_id, event_service=event_service)
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
