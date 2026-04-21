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
    create_employee as repo_create_employee,
)
from src.inventory.employees.repository import (
    create_employee_attribute as repo_create_employee_attribute,
)
from src.inventory.employees.repository import (
    delete_employee_attribute as repo_delete_employee_attribute,
)
from src.inventory.employees.repository import (
    get_employee_by_id as repo_get_employee_by_id,
)
from src.inventory.employees.repository import (
    list_employee_attributes as repo_list_employee_attributes,
)
from src.inventory.employees.repository import (
    list_employees as repo_list_employees,
)
from src.inventory.persons.repository import get_person_by_id as repo_get_person_by_id
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.employees'


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


class EmployeeService:
    """Orchestrates employee creation, retrieval, attribute write, and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_employee(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
        is_locked: bool = False,
        description: str | None = None,
        correlation_id: str | None = None,
    ) -> Employee:
        """Create an employee and emit inventory.employee.created. Validates person_id exists."""
        person = await repo_get_person_by_id(session, person_id)
        if person is None:
            raise InvalidPersonIdError(person_id)
        employee = await repo_create_employee(
            session,
            person_id=person_id,
            is_locked=is_locked,
            description=description,
        )
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.employee.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'employee_id': str(employee.id),
                    'person_id': str(employee.person_id),
                    'is_locked': employee.is_locked,
                    'description': employee.description,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(employee.id),
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

    async def list_employees(self, session: AsyncSession) -> list[Employee]:
        """List all employees."""
        return await repo_list_employees(session)

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
                actor_kind=EventParticipantKind.CAPABILITY,
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
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(employee.id),
            )
        )
