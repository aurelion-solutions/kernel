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
from src.inventory.subjects.models import Subject, SubjectKind
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

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

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
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.nhi.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'nhi_id': str(nhi.id),
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

    async def _resolve_nhi_subject_id(self, session: AsyncSession, nhi_id: uuid.UUID) -> str:
        """Return the Subject.id string for the given NHI, or nhi_id as fallback."""
        subject_result = await session.execute(
            sa.select(Subject).where(
                Subject.kind == SubjectKind.nhi,
                Subject.principal_nhi_id == nhi_id,
            )
        )
        subject = subject_result.scalar_one_or_none()
        return str(subject.id) if subject is not None else str(nhi_id)

    async def update_nhi(
        self,
        session: AsyncSession,
        nhi_id: uuid.UUID,
        patch: NHIPatch,
        correlation_id: str | None = None,
    ) -> NHI:
        """PATCH NHI fields and/or attributes.

        Context fields (name, application_id, owner_employee_id) emit
        subject.context.changed (subject_type=nhi).
        Attribute changes also emit subject.context.changed.
        Non-context fields (description, is_locked) emit no domain event.
        """
        nhi = await repo_get_nhi_by_id(session, nhi_id)
        if nhi is None:
            raise NHINotFoundError(nhi_id)

        context_changed = False

        if patch.name is not None:
            nhi.name = patch.name
            context_changed = True
        if patch.description is not None:
            nhi.description = patch.description
        if patch.is_locked is not None:
            nhi.is_locked = patch.is_locked
        if patch.owner_employee_id is not None:
            emp = await repo_get_employee_by_id(session, patch.owner_employee_id)
            if emp is None:
                raise InvalidOwnerEmployeeIdError(patch.owner_employee_id)
            nhi.owner_employee_id = patch.owner_employee_id
            context_changed = True
        if patch.application_id is not None:
            app = await repo_get_application_by_id(session, patch.application_id)
            if app is None:
                raise InvalidApplicationIdError(patch.application_id)
            nhi.application_id = patch.application_id
            context_changed = True

        if patch.attributes is not None:
            context_changed = True
            for key, value in patch.attributes.items():
                try:
                    await repo_create_nhi_attribute(session, nhi_id=nhi_id, key=key, value=value)
                except IntegrityError:
                    # Attribute already exists — update in place
                    await session.execute(
                        sa.update(NHIAttribute)
                        .where(NHIAttribute.nhi_id == nhi_id, NHIAttribute.key == key)
                        .values(value=value)
                    )

        await session.flush()
        await session.refresh(nhi)

        if context_changed:
            subject_id = await self._resolve_nhi_subject_id(session, nhi_id)
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='subject.context.changed',
                    occurred_at=datetime.now(UTC),
                    correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                    causation_id=None,
                    payload={
                        'subject_id': subject_id,
                        'subject_type': 'nhi',
                        'nhi_id': str(nhi_id),
                    },
                    actor_kind=EventParticipantKind.COMPONENT,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=subject_id,
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

        subject_id = await self._resolve_nhi_subject_id(session, nhi_id)

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.nhi.expired',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'nhi_id': str(nhi_id),
                    'subject_id': subject_id,
                    'subject_type': 'nhi',
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(nhi_id),
            )
        )
        return nhi
