# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee service for coordinating repository and log emission."""

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
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service


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
    """Orchestrates employee CRUD and log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_employee(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
        is_locked: bool = False,
        description: str | None = None,
    ) -> Employee:
        """Create an employee and emit employee.created. Validates person_id exists."""
        person = await repo_get_person_by_id(session, person_id)
        if person is None:
            raise InvalidPersonIdError(person_id)
        employee = await repo_create_employee(
            session,
            person_id=person_id,
            is_locked=is_locked,
            description=description,
        )
        self._log.emit_safe(
            'employee.created',
            LogLevel.INFO,
            'Employee created',
            'identity-core',
            merge_emit_log_participant_fields(
                {'employee_id': str(employee.id)},
                actor_component='identity-core',
                target_id='employee',
            ),
        )
        return employee

    async def get_employee(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
    ) -> Employee | None:
        """Get employee by id. Emits employee.retrieved when found."""
        employee = await repo_get_employee_by_id(session, employee_id)
        if employee is not None:
            self._log.emit_safe(
                'employee.retrieved',
                LogLevel.INFO,
                'Employee retrieved',
                'identity-core',
                merge_emit_log_participant_fields(
                    {'employee_id': str(employee_id)},
                    actor_component='identity-core',
                    target_id='employee',
                ),
            )
        return employee

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
    ) -> EmployeeAttribute:
        """Add attribute to employee. Emits employee.attribute.added. Raises on duplicate key."""
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
        self._log.emit_safe(
            'employee.attribute.added',
            LogLevel.INFO,
            'Employee attribute added',
            'identity-core',
            merge_emit_log_participant_fields(
                {
                    'employee_id': str(employee_id),
                    'key': key,
                },
                actor_component='identity-core',
                target_id='employee',
            ),
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        employee_id: uuid.UUID,
        key: str,
    ) -> None:
        """Remove attribute from employee. Emits employee.attribute.removed. Raises if not found."""
        employee = await repo_get_employee_by_id(session, employee_id)
        if employee is None:
            raise EmployeeNotFoundError(employee_id)
        deleted = await repo_delete_employee_attribute(session, employee_id, key)
        if not deleted:
            raise EmployeeAttributeNotFoundError(employee_id, key)
        self._log.emit_safe(
            'employee.attribute.removed',
            LogLevel.INFO,
            'Employee attribute removed',
            'identity-core',
            merge_emit_log_participant_fields(
                {'employee_id': str(employee_id), 'key': key},
                actor_component='identity-core',
                target_id='employee',
            ),
        )
