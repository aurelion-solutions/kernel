# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""NHI service for coordinating repository and log emission."""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employees.repository import get_employee_by_id as repo_get_employee_by_id
from src.inventory.nhi.models import NHI, NHIAttribute
from src.inventory.nhi.repository import (
    create_nhi as repo_create_nhi,
)
from src.inventory.nhi.repository import (
    create_nhi_attribute as repo_create_nhi_attribute,
)
from src.inventory.nhi.repository import (
    delete_nhi_attribute as repo_delete_nhi_attribute,
)
from src.inventory.nhi.repository import (
    get_nhi_by_id as repo_get_nhi_by_id,
)
from src.inventory.nhi.repository import (
    list_nhi as repo_list_nhi,
)
from src.inventory.nhi.repository import (
    list_nhi_attributes as repo_list_nhi_attributes,
)
from src.platform.applications.repository import get_application_by_id as repo_get_application_by_id
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service


class NHINotFoundError(Exception):
    """Raised when an NHI is not found."""

    def __init__(self, nhi_id: uuid.UUID) -> None:
        self.nhi_id = nhi_id
        super().__init__(f'NHI not found: {nhi_id}')


class InvalidOwnerEmployeeIdError(Exception):
    """Raised when owner_employee_id does not reference an existing Employee."""

    def __init__(self, owner_employee_id: uuid.UUID) -> None:
        self.owner_employee_id = owner_employee_id
        super().__init__(f'Employee not found: {owner_employee_id}')


class InvalidApplicationIdError(Exception):
    """Raised when application_id does not reference an existing Application."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application not found: {application_id}')


class NHIAttributeNotFoundError(Exception):
    """Raised when an NHI attribute is not found."""

    def __init__(self, nhi_id: uuid.UUID, key: str) -> None:
        self.nhi_id = nhi_id
        self.key = key
        super().__init__(f'NHI attribute not found: {nhi_id} / {key}')


class DuplicateNHIAttributeError(Exception):
    """Raised when adding an attribute with a key that already exists for the NHI."""

    def __init__(self, nhi_id: uuid.UUID, key: str) -> None:
        self.nhi_id = nhi_id
        self.key = key
        super().__init__(f'Duplicate attribute key for NHI: {key}')


class NHIService:
    """Orchestrates NHI CRUD and log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_nhi(
        self,
        session: AsyncSession,
        external_id: str,
        name: str,
        kind: str,
        description: str | None = None,
        is_locked: bool = False,
        owner_employee_id: uuid.UUID | None = None,
        application_id: uuid.UUID | None = None,
    ) -> NHI:
        """Create an NHI and emit nhi.created. Validates optional FKs when set."""
        if owner_employee_id is not None:
            emp = await repo_get_employee_by_id(session, owner_employee_id)
            if emp is None:
                raise InvalidOwnerEmployeeIdError(owner_employee_id)
        if application_id is not None:
            app = await repo_get_application_by_id(session, application_id)
            if app is None:
                raise InvalidApplicationIdError(application_id)
        nhi = await repo_create_nhi(
            session,
            external_id=external_id,
            name=name,
            kind=kind,
            description=description,
            is_locked=is_locked,
            owner_employee_id=owner_employee_id,
            application_id=application_id,
        )
        self._log.emit_safe(
            'nhi.created',
            LogLevel.INFO,
            'NHI created',
            'identity-core',
            merge_emit_log_participant_fields(
                {
                    'nhi_id': str(nhi.id),
                    'external_id': nhi.external_id,
                },
                actor_component='identity-core',
                target_id='nhi',
            ),
        )
        return nhi

    async def get_nhi(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
    ) -> NHI | None:
        """Get NHI by id. Emits nhi.retrieved when found."""
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is not None:
            self._log.emit_safe(
                'nhi.retrieved',
                LogLevel.INFO,
                'NHI retrieved',
                'identity-core',
                merge_emit_log_participant_fields(
                    {'nhi_id': str(nhi_id)},
                    actor_component='identity-core',
                    target_id='nhi',
                ),
            )
        return nhi

    async def list_nhi(self, session: AsyncSession) -> list[NHI]:
        """List all NHIs."""
        return await repo_list_nhi(session)

    async def list_attributes(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
    ) -> list[NHIAttribute]:
        """List attributes for an NHI. Raises NHINotFoundError if NHI missing."""
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is None:
            raise NHINotFoundError(nhi_id)
        return await repo_list_nhi_attributes(session, nhi_id)

    async def add_attribute(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
        key: str,
        value: str,
    ) -> NHIAttribute:
        """Add attribute to NHI. Emits nhi.attribute.added. Raises on duplicate key."""
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is None:
            raise NHINotFoundError(nhi_id)
        try:
            attr = await repo_create_nhi_attribute(
                session,
                nhi_id=nhi_id,
                key=key,
                value=value,
            )
        except IntegrityError:
            raise DuplicateNHIAttributeError(nhi_id, key) from None
        self._log.emit_safe(
            'nhi.attribute.added',
            LogLevel.INFO,
            'NHI attribute added',
            'identity-core',
            merge_emit_log_participant_fields(
                {
                    'nhi_id': str(nhi_id),
                    'key': key,
                },
                actor_component='identity-core',
                target_id='nhi',
            ),
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
        key: str,
    ) -> None:
        """Remove attribute from NHI. Emits nhi.attribute.removed. Raises if not found."""
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is None:
            raise NHINotFoundError(nhi_id)
        deleted = await repo_delete_nhi_attribute(session, nhi_id, key)
        if not deleted:
            raise NHIAttributeNotFoundError(nhi_id, key)
        self._log.emit_safe(
            'nhi.attribute.removed',
            LogLevel.INFO,
            'NHI attribute removed',
            'identity-core',
            merge_emit_log_participant_fields(
                {'nhi_id': str(nhi_id), 'key': key},
                actor_component='identity-core',
                target_id='nhi',
            ),
        )
