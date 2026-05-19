# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.employees.repository import (
    EmployeeUpsertData,
)
from src.inventory.employees.repository import (
    bulk_upsert_employees as repo_bulk_upsert_employees,
)
from src.inventory.employees.repository import (
    create_employee as repo_create_employee,
)
from src.inventory.employees.repository import (
    create_employee_attribute as repo_create_employee_attribute,
)
from src.inventory.employees.repository import (
    delete_employee_attribute as repo_delete_employee_attribute,
)
from src.inventory.employees.repository import (
    get_employee_attribute_by_key as repo_get_employee_attribute_by_key,
)
from src.inventory.employees.repository import (
    get_employee_by_id as repo_get_employee_by_id,
)
from src.inventory.employees.repository import (
    list_employee_attributes as repo_list_employee_attributes,
)
from src.inventory.employees.repository import (
    list_employees_page as repo_list_employees_page,
)
from src.inventory.employees.repository import (
    org_unit_exists as repo_org_unit_exists,
)
from src.inventory.employees.repository import (
    resolve_persons_by_external_ids as repo_resolve_persons_by_external_ids,
)
from src.inventory.employees.repository import (
    upsert_employee_attribute as repo_upsert_employee_attribute,
)
from src.inventory.employees.schemas import EmployeeBulkItem, EmployeePatch
from src.inventory.org_units.repository import (
    get_by_external_ids as repo_get_org_units_by_external_ids,
)
from src.inventory.persons.repository import get_person_by_id as repo_get_person_by_id
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.service import SubjectService
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.employees'


class UnknownPersonExternalIdsError(Exception):
    """Raised when one or more person_external_ids are not found in persons."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f'Unknown person_external_ids: {", ".join(missing)}')


class EmployeeNotFoundError(Exception):
    """Raised when an employee is not found."""

    def __init__(self, employee_id: uuid.UUID) -> None:
        self.employee_id = employee_id
        super().__init__(f'Employee not found: {employee_id}')


class InvalidPersonIdError(Exception):
    """Raised when person_id does not reference an existing Person."""

    def __init__(self, person_id: uuid.UUID) -> None:
        self.person_id = person_id
        super().__init__(f'Person not found: {person_id}')


class EmployeeAttributeNotFoundError(Exception):
    """Raised when an employee attribute is not found."""

    def __init__(self, employee_id: uuid.UUID, key: str) -> None:
        self.employee_id = employee_id
        self.key = key
        super().__init__(f'Employee attribute not found: {employee_id} / {key}')


class DuplicateEmployeeAttributeError(Exception):
    """Raised when adding an attribute with a key that already exists for the employee."""

    def __init__(self, employee_id: uuid.UUID, key: str) -> None:
        self.employee_id = employee_id
        self.key = key
        super().__init__(f'Duplicate attribute key for employee: {key}')


class EmployeeOrgUnitNotFoundError(Exception):
    """Raised when one or more org_unit_external_ids cannot be resolved to known org_units."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f'Unknown org_unit_external_ids: {", ".join(missing)}')


class InvalidOrgUnitIdError(Exception):
    """Raised when org_unit_id does not reference an existing OrgUnit."""

    def __init__(self, org_unit_id: uuid.UUID) -> None:
        self.org_unit_id = org_unit_id
        super().__init__(f'Org-unit not found: {org_unit_id}')


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _build_employee_created_event(
    employee_id: uuid.UUID,
    subject_id: uuid.UUID,
    person_id: uuid.UUID,
    is_locked: bool,
    description: str | None,
    org_unit_id: uuid.UUID | None,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build ``inventory.employee.created`` event.

    Payload contract:
      - employee_id: str(uuid)
      - subject_ref: str(Subject.id)
      - subject_type: 'employee'
      - person_id, is_locked, description, org_unit_id
    """
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.employee.created',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'employee_id': str(employee_id),
            'subject_ref': str(subject_id),
            'subject_type': 'employee',
            'person_id': str(person_id),
            'is_locked': is_locked,
            'description': description,
            'org_unit_id': str(org_unit_id) if org_unit_id is not None else None,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(employee_id),
    )


def _build_employee_updated_event(
    employee_id: uuid.UUID,
    subject_id: uuid.UUID,
    changes: dict[str, dict[str, object | None]],
    correlation_id: str,
) -> EventEnvelope:
    """Build the unified ``inventory.employee.updated`` event.

    Payload contract:
      - employee_id: str(uuid)
      - subject_ref: str(Subject.id)
      - subject_type: 'employee'
      - changes: {field: {'old': ..., 'new': ...}}
    """
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.employee.updated',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'employee_id': str(employee_id),
            'subject_ref': str(subject_id),
            'subject_type': 'employee',
            'changes': changes,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(employee_id),
    )


class EmployeeService:
    """Orchestrates employee creation, retrieval, attribute write, and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
        subject_service: SubjectService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service
        self._subject_service = (
            subject_service if subject_service is not None else SubjectService(event_service=event_service)
        )

    async def create_employee(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
        is_locked: bool = False,
        description: str | None = None,
        org_unit_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> Employee:
        """Create an employee and emit inventory.employee.created. Validates person_id and org_unit_id exist."""
        person = await repo_get_person_by_id(session, person_id)
        if person is None:
            raise InvalidPersonIdError(person_id)
        if org_unit_id is not None:
            exists = await repo_org_unit_exists(session, org_unit_id)
            if not exists:
                raise InvalidOrgUnitIdError(org_unit_id)
        employee = await repo_create_employee(
            session,
            person_id=person_id,
            is_locked=is_locked,
            description=description,
            org_unit_id=org_unit_id,
        )
        subject = await self._subject_service.ensure_for_principal(
            session,
            kind=SubjectKind.employee,
            principal_id=employee.id,
            correlation_id=correlation_id,
        )
        await self._events.emit(
            _build_employee_created_event(
                employee_id=employee.id,
                subject_id=subject.id,
                person_id=employee.person_id,
                is_locked=employee.is_locked,
                description=employee.description,
                org_unit_id=employee.org_unit_id,
                correlation_id=correlation_id,
            )
        )
        return employee

    async def get_employee(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
    ) -> Employee | None:
        """Get employee by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_employee_by_id(session, employee_id)

    async def list_employees(
        self,
        session: AsyncSession,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[Employee], int]:
        """Return (rows, total) for paginated GET /employees."""
        return await repo_list_employees_page(session, limit=limit, offset=offset)

    async def list_attributes(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
    ) -> list[EmployeeAttribute]:
        """List attributes for an employee. Raises EmployeeNotFoundError if employee missing."""
        employee = await repo_get_employee_by_id(session, employee_id)
        if employee is None:
            raise EmployeeNotFoundError(employee_id)
        return await repo_list_employee_attributes(session, employee_id)

    async def add_attribute(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
        key: str,
        value: str,
        correlation_id: str | None = None,
    ) -> EmployeeAttribute:
        """Add attribute to employee. Emits inventory.employee.attribute_added. Raises on duplicate key."""
        employee = await repo_get_employee_by_id(session, employee_id)
        if employee is None:
            raise EmployeeNotFoundError(employee_id)
        try:
            attr = await repo_create_employee_attribute(
                session,
                employee_id=employee_id,
                key=key,
                value=value,
            )
        except IntegrityError:
            raise DuplicateEmployeeAttributeError(employee_id, key) from None
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.employee.attribute_added',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'employee_id': str(employee_id),
                    'attribute_id': str(attr.id),
                    'key': key,
                    'value': value,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(employee.id),
            )
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
        key: str,
        correlation_id: str | None = None,
    ) -> None:
        """Remove attribute from employee. Emits inventory.employee.attribute_removed. Raises if not found."""
        employee = await repo_get_employee_by_id(session, employee_id)
        if employee is None:
            raise EmployeeNotFoundError(employee_id)
        deleted = await repo_delete_employee_attribute(session, employee_id, key)
        if not deleted:
            raise EmployeeAttributeNotFoundError(employee_id, key)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.employee.attribute_removed',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'employee_id': str(employee_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(employee.id),
            )
        )

    async def update_employee(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
        patch: EmployeePatch,
        correlation_id: str | None = None,
    ) -> Employee:
        """Patch employee fields and emit one ``inventory.employee.updated`` event.

        Emits a single fat event per call with ``changes: {field: {old, new}}``.
        ``attributes.<key>`` shows up as ``changes["attributes.<key>"]``.
        If nothing actually changes (every field already at the target value),
        no event is emitted.
        """
        employee = await repo_get_employee_by_id(session, employee_id)
        if employee is None:
            raise EmployeeNotFoundError(employee_id)

        corr_id = correlation_id if correlation_id is not None else uuid.uuid4().hex
        changes: dict[str, dict[str, object | None]] = {}

        if patch.org_unit_id is not None and employee.org_unit_id != patch.org_unit_id:
            changes['org_unit_id'] = {
                'old': str(employee.org_unit_id) if employee.org_unit_id is not None else None,
                'new': str(patch.org_unit_id),
            }
            employee.org_unit_id = patch.org_unit_id

        if patch.description is not None and employee.description != patch.description:
            changes['description'] = {
                'old': employee.description,
                'new': patch.description,
            }
            employee.description = patch.description

        if patch.attributes is not None:
            for key, value in patch.attributes.items():
                old_attr = await repo_get_employee_attribute_by_key(session, employee_id, key)
                old_value = old_attr.value if old_attr is not None else None
                if old_value == value:
                    continue
                await repo_upsert_employee_attribute(session, employee_id=employee_id, key=key, value=value)
                changes[f'attributes.{key}'] = {'old': old_value, 'new': value}

        await session.flush()

        if changes:
            subject = await self._subject_service.ensure_for_principal(
                session,
                kind=SubjectKind.employee,
                principal_id=employee_id,
                correlation_id=corr_id,
            )
            await self._events.emit(
                _build_employee_updated_event(
                    employee_id=employee_id,
                    subject_id=subject.id,
                    changes=changes,
                    correlation_id=corr_id,
                )
            )

        return employee

    async def bulk_upsert_employees(
        self,
        session: AsyncSession,
        items: list[EmployeeBulkItem],
        correlation_id: str | None = None,
    ) -> list[Employee]:
        """Bulk-upsert employees by person_external_id. Emits inventory.employee.bulk_upserted.

        Raises:
            UnknownPersonExternalIdsError: if any person_external_id is not found.

        """
        external_ids = [item.person_external_id for item in items]
        mapping = await repo_resolve_persons_by_external_ids(session, external_ids)

        missing = [eid for eid in external_ids if eid not in mapping]
        if missing:
            raise UnknownPersonExternalIdsError(missing)

        # Resolve org_unit_external_id → org_unit_id (one batch SELECT IN).
        ou_external_ids = [item.org_unit_external_id for item in items if item.org_unit_external_id is not None]
        ou_id_map: dict[str, uuid.UUID] = {}
        if ou_external_ids:
            ou_id_map = await repo_get_org_units_by_external_ids(session, ou_external_ids)
            missing_ou = [eid for eid in ou_external_ids if eid not in ou_id_map]
            if missing_ou:
                raise EmployeeOrgUnitNotFoundError(missing_ou)

        upsert_rows = [
            EmployeeUpsertData(
                person_id=mapping[item.person_external_id],
                is_locked=item.is_locked,
                description=item.description,
                org_unit_id=ou_id_map.get(item.org_unit_external_id) if item.org_unit_external_id is not None else None,
                attributes=dict(item.attributes) if item.attributes else {},
            )
            for item in items
        ]
        employees = await repo_bulk_upsert_employees(session, upsert_rows)

        for employee in employees:
            await self._subject_service.ensure_for_principal(
                session,
                kind=SubjectKind.employee,
                principal_id=employee.id,
                correlation_id=correlation_id,
            )

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.employee.bulk_upserted',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'count': len(employees),
                    'person_ids': [str(e.person_id) for e in employees],
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=_COMPONENT,
            )
        )
        return employees
