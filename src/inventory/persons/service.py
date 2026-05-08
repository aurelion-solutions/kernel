# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.persons.models import Person, PersonAttribute
from src.inventory.persons.repository import (
    bulk_upsert_persons as repo_bulk_upsert_persons,
)
from src.inventory.persons.repository import (
    create_person as repo_create_person,
)
from src.inventory.persons.repository import (
    create_person_attribute as repo_create_person_attribute,
)
from src.inventory.persons.repository import (
    delete_person_attribute as repo_delete_person_attribute,
)
from src.inventory.persons.repository import (
    get_person_by_id as repo_get_person_by_id,
)
from src.inventory.persons.repository import (
    list_person_attributes as repo_list_person_attributes,
)
from src.inventory.persons.repository import (
    list_persons as repo_list_persons,
)
from src.inventory.persons.schemas import PersonBulkItem
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.persons'


class PersonNotFoundError(Exception):
    """Raised when a person is not found."""

    def __init__(self, person_id: uuid.UUID) -> None:
        self.person_id = person_id
        super().__init__(f'Person not found: {person_id}')


class PersonAttributeNotFoundError(Exception):
    """Raised when a person attribute is not found."""

    def __init__(self, person_id: uuid.UUID, key: str) -> None:
        self.person_id = person_id
        self.key = key
        super().__init__(f'Person attribute not found: {person_id} / {key}')


class DuplicatePersonAttributeError(Exception):
    """Raised when adding an attribute with a key that already exists for the person."""

    def __init__(self, person_id: uuid.UUID, key: str) -> None:
        self.person_id = person_id
        self.key = key
        super().__init__(f'Duplicate attribute key for person: {key}')


class PersonService:
    """Orchestrates person creation, retrieval, attribute write, and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_person(
        self,
        session: AsyncSession,
        external_id: str,
        full_name: str,
        correlation_id: str | None = None,
    ) -> Person:
        """Create a person and emit inventory.person.created."""
        person = await repo_create_person(
            session,
            external_id=external_id,
            full_name=full_name,
        )
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.person.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'person_id': str(person.id),
                    'external_id': person.external_id,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(person.id),
            )
        )
        return person

    async def get_person(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
    ) -> Person | None:
        """Get person by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_person_by_id(session, person_id)

    async def list_persons(self, session: AsyncSession) -> list[Person]:
        """List all persons."""
        return await repo_list_persons(session)

    async def list_attributes(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
    ) -> list[PersonAttribute]:
        """List attributes for a person. Raises PersonNotFoundError if person missing."""
        person = await repo_get_person_by_id(session, person_id)
        if person is None:
            raise PersonNotFoundError(person_id)
        return await repo_list_person_attributes(session, person_id)

    async def add_attribute(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
        key: str,
        value: str,
        correlation_id: str | None = None,
    ) -> PersonAttribute:
        """Add attribute to person. Emits inventory.person.attribute_added. Raises on duplicate key."""
        person = await repo_get_person_by_id(session, person_id)
        if person is None:
            raise PersonNotFoundError(person_id)
        try:
            attr = await repo_create_person_attribute(
                session,
                person_id=person_id,
                key=key,
                value=value,
            )
        except IntegrityError:
            raise DuplicatePersonAttributeError(person_id, key) from None
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.person.attribute_added',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'person_id': str(person_id),
                    'attribute_id': str(attr.id),
                    'key': key,
                    'value': value,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(person.id),
            )
        )
        return attr

    async def bulk_upsert_persons(
        self,
        session: AsyncSession,
        items: list[PersonBulkItem],
        correlation_id: str | None = None,
    ) -> list[Person]:
        """Bulk-upsert persons by external_id and emit a single domain event."""
        pairs = [(item.external_id, item.full_name) for item in items]
        persons = await repo_bulk_upsert_persons(session, pairs)
        await session.flush()
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.person.bulk_upserted',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'count': len(persons),
                    'external_ids': [p.external_id for p in persons],
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id='persons',
            )
        )
        return persons

    async def remove_attribute(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
        key: str,
        correlation_id: str | None = None,
    ) -> None:
        """Remove attribute from person. Emits inventory.person.attribute_removed. Raises if not found."""
        person = await repo_get_person_by_id(session, person_id)
        if person is None:
            raise PersonNotFoundError(person_id)
        deleted = await repo_delete_person_attribute(session, person_id, key)
        if not deleted:
            raise PersonAttributeNotFoundError(person_id, key)
        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.person.attribute_removed',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'person_id': str(person_id),
                    'key': key,
                },
                actor_kind=EventParticipantKind.COMPONENT,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(person.id),
            )
        )
