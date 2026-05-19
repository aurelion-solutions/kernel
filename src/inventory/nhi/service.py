# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""NHI service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import sqlalchemy as sa
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
from src.inventory.nhi.schemas import NHIPatch
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.service import SubjectService
from src.platform.applications.repository import get_application_by_id as repo_get_application_by_id
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.nhi'


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
    """Orchestrates NHI CRUD and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
        subject_service: SubjectService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service
        self._subject_service = (
            subject_service if subject_service is not None else SubjectService(event_service=event_service)
        )

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
        correlation_id: str | None = None,
    ) -> NHI:
        """Create an NHI. Emits inventory.nhi.created. Validates optional FKs when set."""
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
        subject = await self._subject_service.ensure_for_principal(
            session,
            kind=SubjectKind.nhi,
            principal_id=nhi.id,
            correlation_id=correlation_id,
        )
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.nhi.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'nhi_id': str(nhi.id),
                    'subject_ref': str(subject.id),
                    'subject_type': 'nhi',
                    'external_id': nhi.external_id,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(nhi.id),
            )
        )
        return nhi

    async def get_nhi(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
    ) -> NHI | None:
        """Get NHI by id. No event emitted (Q1 — nhi.retrieved dropped, audit deferred to future audit.* slice)."""
        return await repo_get_nhi_by_id(session, nhi_id)

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
        correlation_id: str | None = None,
    ) -> NHIAttribute:
        """Add attribute to NHI. Emits inventory.nhi.attribute_added. Raises on duplicate key."""
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
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.nhi.attribute_added',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'nhi_id': str(nhi_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(nhi_id),
            )
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
        key: str,
        correlation_id: str | None = None,
    ) -> None:
        """Remove attribute from NHI. Emits inventory.nhi.attribute_removed. Raises if not found."""
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is None:
            raise NHINotFoundError(nhi_id)
        deleted = await repo_delete_nhi_attribute(session, nhi_id, key)
        if not deleted:
            raise NHIAttributeNotFoundError(nhi_id, key)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.nhi.attribute_removed',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'nhi_id': str(nhi_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(nhi_id),
            )
        )

    async def update_nhi(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
        patch: NHIPatch,
        correlation_id: str | None = None,
    ) -> NHI:
        """PATCH NHI fields and/or attributes.

        Emits one fat ``inventory.nhi.updated`` event with payload
        ``{nhi_id, subject_ref, subject_type, changes}``. Attribute updates
        appear in ``changes`` under key ``attributes.<key>``. No event is
        emitted when nothing actually changes.
        """
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is None:
            raise NHINotFoundError(nhi_id)

        changes: dict[str, dict[str, object | None]] = {}

        if patch.name is not None and nhi.name != patch.name:
            changes['name'] = {'old': nhi.name, 'new': patch.name}
            nhi.name = patch.name
        if patch.description is not None and nhi.description != patch.description:
            changes['description'] = {'old': nhi.description, 'new': patch.description}
            nhi.description = patch.description
        if patch.is_locked is not None and nhi.is_locked != patch.is_locked:
            changes['is_locked'] = {'old': nhi.is_locked, 'new': patch.is_locked}
            nhi.is_locked = patch.is_locked
        if patch.owner_employee_id is not None and nhi.owner_employee_id != patch.owner_employee_id:
            emp = await repo_get_employee_by_id(session, patch.owner_employee_id)
            if emp is None:
                raise InvalidOwnerEmployeeIdError(patch.owner_employee_id)
            changes['owner_employee_id'] = {
                'old': str(nhi.owner_employee_id) if nhi.owner_employee_id is not None else None,
                'new': str(patch.owner_employee_id),
            }
            nhi.owner_employee_id = patch.owner_employee_id
        if patch.application_id is not None and nhi.application_id != patch.application_id:
            app = await repo_get_application_by_id(session, patch.application_id)
            if app is None:
                raise InvalidApplicationIdError(patch.application_id)
            changes['application_id'] = {
                'old': str(nhi.application_id) if nhi.application_id is not None else None,
                'new': str(patch.application_id),
            }
            nhi.application_id = patch.application_id

        if patch.attributes is not None:
            for key, value in patch.attributes.items():
                existing_result = await session.execute(
                    sa.select(NHIAttribute).where(NHIAttribute.nhi_id == nhi_id, NHIAttribute.key == key)
                )
                existing_attr = existing_result.scalar_one_or_none()
                old_value = existing_attr.value if existing_attr is not None else None
                if old_value == value:
                    continue
                if existing_attr is None:
                    await repo_create_nhi_attribute(session, nhi_id=nhi_id, key=key, value=value)
                else:
                    await session.execute(
                        sa.update(NHIAttribute)
                        .where(NHIAttribute.nhi_id == nhi_id, NHIAttribute.key == key)
                        .values(value=value)
                    )
                changes[f'attributes.{key}'] = {'old': old_value, 'new': value}

        await session.flush()
        await session.refresh(nhi)

        if changes:
            subject = await self._subject_service.ensure_for_principal(
                session,
                kind=SubjectKind.nhi,
                principal_id=nhi_id,
                correlation_id=correlation_id,
            )
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='inventory.nhi.updated',
                    occurred_at=datetime.now(UTC),
                    correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                    causation_id=None,
                    payload={
                        'nhi_id': str(nhi_id),
                        'subject_ref': str(subject.id),
                        'subject_type': 'nhi',
                        'changes': changes,
                    },
                    actor_kind=EventParticipantKind.COMPONENT,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=str(subject.id),
                )
            )
        return nhi

    async def deactivate_nhi(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> NHI:
        """Deactivate (expire) an NHI.

        Sets is_locked=True and emits inventory.nhi.expired.
        Raises NHINotFoundError if the NHI does not exist.
        """
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is None:
            raise NHINotFoundError(nhi_id)

        nhi.is_locked = True
        await session.flush()
        await session.refresh(nhi)

        subject = await self._subject_service.ensure_for_principal(
            session,
            kind=SubjectKind.nhi,
            principal_id=nhi_id,
            correlation_id=correlation_id,
        )

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.nhi.expired',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'nhi_id': str(nhi_id),
                    'subject_ref': str(subject.id),
                    'subject_type': 'nhi',
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(subject.id),
            )
        )
        return nhi
