# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmployeeRecord service for coordinating repository and log emission."""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employee_records.models import (
    EmployeeRecord,
    EmployeeRecordAttribute,
)
from src.inventory.employee_records.repository import (
    create_employee_record as repo_create_employee_record,
)
from src.inventory.employee_records.repository import (
    create_employee_record_attribute as repo_create_employee_record_attribute,
)
from src.inventory.employee_records.repository import (
    delete_employee_record_attribute as repo_delete_employee_record_attribute,
)
from src.inventory.employee_records.repository import (
    get_employee_record_by_id as repo_get_employee_record_by_id,
)
from src.inventory.employee_records.repository import (
    list_employee_record_attributes as repo_list_employee_record_attributes,
)
from src.inventory.employee_records.repository import (
    list_employee_records as repo_list_employee_records,
)
from src.platform.applications.repository import get_application_by_id as repo_get_application_by_id
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service


class EmployeeRecordNotFoundError(Exception):
    """Raised when an employee record is not found."""

    def __init__(self, employee_record_id: uuid.UUID) -> None:
        self.employee_record_id = employee_record_id
        super().__init__(f'Employee record not found: {employee_record_id}')


class InvalidApplicationIdError(Exception):
    """Raised when application_id does not reference an existing Application."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application not found: {application_id}')


class EmployeeRecordAttributeNotFoundError(Exception):
    """Raised when an employee record attribute is not found."""

    def __init__(self, employee_record_id: uuid.UUID, key: str) -> None:
        self.employee_record_id = employee_record_id
        self.key = key
        super().__init__(f'Employee record attribute not found: {employee_record_id} / {key}')


class DuplicateEmployeeRecordAttributeError(Exception):
    """Raised when adding an attribute with a key that already exists for the record."""

    def __init__(self, employee_record_id: uuid.UUID, key: str) -> None:
        self.employee_record_id = employee_record_id
        self.key = key
        super().__init__(f'Duplicate attribute key for employee record: {key}')


class EmployeeRecordService:
    """Orchestrates employee record CRUD and log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_employee_record(
        self,
        session: AsyncSession,
        external_id: str,
        application_id: uuid.UUID,
        description: str | None = None,
    ) -> EmployeeRecord:
        """Create an employee record and emit employee_record.created. Validates application_id."""
        app = await repo_get_application_by_id(session, application_id)
        if app is None:
            raise InvalidApplicationIdError(application_id)
        record = await repo_create_employee_record(
            session,
            external_id=external_id,
            application_id=application_id,
            description=description,
        )
        self._log.emit_safe(
            'employee_record.created',
            LogLevel.INFO,
            'Employee record created',
            'identity-core',
            merge_emit_log_participant_fields(
                {
                    'employee_record_id': str(record.id),
                    'external_id': record.external_id,
                },
                actor_component='identity-core',
                target_id='employee_record',
            ),
        )
        return record

    async def get_employee_record(
        self,
        session: AsyncSession,
        employee_record_id: uuid.UUID,
    ) -> EmployeeRecord | None:
        """Get employee record by id. Emits employee_record.retrieved when found."""
        record = await repo_get_employee_record_by_id(session, employee_record_id)
        if record is not None:
            self._log.emit_safe(
                'employee_record.retrieved',
                LogLevel.INFO,
                'Employee record retrieved',
                'identity-core',
                merge_emit_log_participant_fields(
                    {'employee_record_id': str(employee_record_id)},
                    actor_component='identity-core',
                    target_id='employee_record',
                ),
            )
        return record

    async def list_employee_records(self, session: AsyncSession) -> list[EmployeeRecord]:
        """List all employee records."""
        return await repo_list_employee_records(session)

    async def list_attributes(
        self,
        session: AsyncSession,
        employee_record_id: uuid.UUID,
    ) -> list[EmployeeRecordAttribute]:
        """List attributes for an employee record. Raises EmployeeRecordNotFoundError if missing."""
        record = await repo_get_employee_record_by_id(session, employee_record_id)
        if record is None:
            raise EmployeeRecordNotFoundError(employee_record_id)
        return await repo_list_employee_record_attributes(session, employee_record_id)

    async def add_attribute(
        self,
        session: AsyncSession,
        employee_record_id: uuid.UUID,
        key: str,
        value: str,
    ) -> EmployeeRecordAttribute:
        """Add attribute to employee record. Emits employee_record.attribute.added. Raises on duplicate key."""
        record = await repo_get_employee_record_by_id(session, employee_record_id)
        if record is None:
            raise EmployeeRecordNotFoundError(employee_record_id)
        try:
            attr = await repo_create_employee_record_attribute(
                session,
                employee_record_id=employee_record_id,
                key=key,
                value=value,
            )
        except IntegrityError:
            raise DuplicateEmployeeRecordAttributeError(employee_record_id, key) from None
        self._log.emit_safe(
            'employee_record.attribute.added',
            LogLevel.INFO,
            'Employee record attribute added',
            'identity-core',
            merge_emit_log_participant_fields(
                {
                    'employee_record_id': str(employee_record_id),
                    'key': key,
                },
                actor_component='identity-core',
                target_id='employee_record',
            ),
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        employee_record_id: uuid.UUID,
        key: str,
    ) -> None:
        """Remove attribute from employee record. Emits employee_record.attribute.removed. Raises if not found."""
        record = await repo_get_employee_record_by_id(session, employee_record_id)
        if record is None:
            raise EmployeeRecordNotFoundError(employee_record_id)
        deleted = await repo_delete_employee_record_attribute(session, employee_record_id, key)
        if not deleted:
            raise EmployeeRecordAttributeNotFoundError(employee_record_id, key)
        self._log.emit_safe(
            'employee_record.attribute.removed',
            LogLevel.INFO,
            'Employee record attribute removed',
            'identity-core',
            merge_emit_log_participant_fields(
                {'employee_record_id': str(employee_record_id), 'key': key},
                actor_component='identity-core',
                target_id='employee_record',
            ),
        )
