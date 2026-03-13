# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person repository for PostgreSQL access."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.persons.models import Person, PersonAttribute


async def create_person(
    session: AsyncSession,
    *,
    external_id: str,
    description: str,
) -> Person:
    """Create and persist a person."""
    person = Person(external_id=external_id, description=description)
    session.add(person)
    await session.flush()
    await session.refresh(person)
    return person


async def get_person_by_id(
    session: AsyncSession,
    person_id: uuid.UUID,
) -> Person | None:
    """Load person by id."""
    result = await session.execute(select(Person).where(Person.id == person_id))
    return result.scalar_one_or_none()


async def get_person_by_external_id(
    session: AsyncSession,
    external_id: str,
) -> Person | None:
    """Load person by external_id."""
    result = await session.execute(select(Person).where(Person.external_id == external_id))
    return result.scalar_one_or_none()


async def list_persons(session: AsyncSession) -> list[Person]:
    """List all persons."""
    result = await session.execute(select(Person).order_by(Person.external_id))
    return list(result.scalars().all())


async def list_person_attributes(
    session: AsyncSession,
    person_id: uuid.UUID,
) -> list[PersonAttribute]:
    """List attributes for a person."""
    result = await session.execute(
        select(PersonAttribute).where(PersonAttribute.person_id == person_id).order_by(PersonAttribute.key)
    )
    return list(result.scalars().all())


async def create_person_attribute(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    key: str,
    value: str,
) -> PersonAttribute:
    """Create and persist a person attribute."""
    attr = PersonAttribute(person_id=person_id, key=key, value=value)
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def get_person_attribute_by_key(
    session: AsyncSession,
    person_id: uuid.UUID,
    key: str,
) -> PersonAttribute | None:
    """Load person attribute by person_id and key."""
    result = await session.execute(
        select(PersonAttribute).where(
            PersonAttribute.person_id == person_id,
            PersonAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def delete_person_attribute(
    session: AsyncSession,
    person_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete person attribute by person_id and key. Returns True if deleted."""
    attr = await get_person_attribute_by_key(session, person_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True
