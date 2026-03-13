# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person service for coordinating repository and log emission."""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.persons.models import Person, PersonAttribute
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
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service


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
    """Orchestrates person CRUD and log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_person(
        self,
        session: AsyncSession,
        external_id: str,
        description: str,
    ) -> Person:
        """Create a person and emit person.created."""
        person = await repo_create_person(
            session,
            external_id=external_id,
            description=description,
        )
        self._log.emit_safe(
            'person.created',
            LogLevel.INFO,
            'Person created',
            'identity-core',
            merge_emit_log_participant_fields(
                {
                    'person_id': str(person.id),
                    'external_id': person.external_id,
                },
                actor_component='identity-core',
                target_id='person',
            ),
        )
        return person

    async def get_person(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
    ) -> Person | None:
        """Get person by id. Emits person.retrieved when found."""
        person = await repo_get_person_by_id(session, person_id)
        if person is not None:
            self._log.emit_safe(
                'person.retrieved',
                LogLevel.INFO,
                'Person retrieved',
                'identity-core',
                merge_emit_log_participant_fields(
                    {'person_id': str(person_id)},
                    actor_component='identity-core',
                    target_id='person',
                ),
            )
        return person

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
    ) -> PersonAttribute:
        """Add attribute to person. Emits person.attribute.added. Raises on duplicate key."""
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
        self._log.emit_safe(
            'person.attribute.added',
            LogLevel.INFO,
            'Person attribute added',
            'identity-core',
            merge_emit_log_participant_fields(
                {
                    'person_id': str(person_id),
                    'key': key,
                },
                actor_component='identity-core',
                target_id='person',
            ),
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        person_id: uuid.UUID,
        key: str,
    ) -> None:
        """Remove attribute from person. Emits person.attribute.removed. Raises if not found."""
        person = await repo_get_person_by_id(session, person_id)
        if person is None:
            raise PersonNotFoundError(person_id)
        deleted = await repo_delete_person_attribute(session, person_id, key)
        if not deleted:
            raise PersonAttributeNotFoundError(person_id, key)
        self._log.emit_safe(
            'person.attribute.removed',
            LogLevel.INFO,
            'Person attribute removed',
            'identity-core',
            merge_emit_log_participant_fields(
                {'person_id': str(person_id), 'key': key},
                actor_component='identity-core',
                target_id='person',
            ),
        )
