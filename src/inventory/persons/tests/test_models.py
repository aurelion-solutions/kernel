# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Person and PersonAttribute models."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from src.inventory.persons.models import Person, PersonAttribute


@pytest.mark.asyncio
async def test_create_person_with_required_fields(session_factory) -> None:
    """Person can be created with required fields."""
    async with session_factory() as session:
        person = Person(
            external_id='ext-123',
            description='Alice Smith',
        )
        session.add(person)
        await session.flush()
        assert person.id is not None
        assert person.external_id == 'ext-123'
        assert person.description == 'Alice Smith'


@pytest.mark.asyncio
async def test_person_id_is_uuid_primary_key(session_factory) -> None:
    """Person id is UUID primary key."""
    async with session_factory() as session:
        person = Person(
            external_id='ext-456',
            description='Bob Jones',
        )
        session.add(person)
        await session.commit()
        assert isinstance(person.id, uuid.UUID)
        assert person.id is not None


@pytest.mark.asyncio
async def test_create_person_attribute_with_required_fields(session_factory) -> None:
    """PersonAttribute can be created with required fields."""
    async with session_factory() as session:
        person = Person(
            external_id='ext-789',
            description='Carol Doe',
        )
        session.add(person)
        await session.flush()

        attr = PersonAttribute(
            person_id=person.id,
            key='department',
            value='Engineering',
        )
        session.add(attr)
        await session.flush()
        assert attr.id is not None
        assert attr.person_id == person.id
        assert attr.key == 'department'
        assert attr.value == 'Engineering'


@pytest.mark.asyncio
async def test_person_attribute_belongs_to_person(session_factory) -> None:
    """PersonAttribute belongs to Person; relationship works both ways."""
    async with session_factory() as session:
        person = Person(
            external_id='ext-rel',
            description='Dave Wilson',
        )
        session.add(person)
        await session.flush()

        attr = PersonAttribute(
            person_id=person.id,
            key='title',
            value='Engineer',
        )
        session.add(attr)
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        result = await session.execute(
            select(Person).where(Person.id == person_id).options(selectinload(Person.attributes))
        )
        loaded = result.scalar_one()
        assert loaded is not None
        assert len(loaded.attributes) == 1
        assert loaded.attributes[0].key == 'title'
        assert loaded.attributes[0].value == 'Engineer'
        assert loaded.attributes[0].person is loaded


@pytest.mark.asyncio
async def test_uniqueness_on_person_id_key_rejected(session_factory) -> None:
    """Duplicate (person_id, key) pair is rejected."""
    async with session_factory() as session:
        person = Person(
            external_id='ext-dup',
            description='Eve Brown',
        )
        session.add(person)
        await session.flush()

        attr1 = PersonAttribute(
            person_id=person.id,
            key='email',
            value='eve@example.com',
        )
        session.add(attr1)
        await session.commit()

    async with session_factory() as session:
        person = (await session.execute(select(Person).where(Person.external_id == 'ext-dup'))).scalar_one()
        attr2 = PersonAttribute(
            person_id=person.id,
            key='email',
            value='different@example.com',
        )
        session.add(attr2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_person(session_factory) -> None:
    """Person requires external_id, description."""
    async with session_factory() as session:
        person = Person(external_id='x')  # missing description
        session.add(person)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_person_attribute_missing_value(
    session_factory,
) -> None:
    """PersonAttribute requires value; missing raises IntegrityError."""
    async with session_factory() as session:
        person = Person(external_id='ext-req', description='Frank')
        session.add(person)
        await session.flush()

        attr = PersonAttribute(person_id=person.id, key='k', value=None)  # type: ignore[arg-type]
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_person_attribute_missing_key(
    session_factory,
) -> None:
    """PersonAttribute requires key; missing raises IntegrityError."""
    async with session_factory() as session:
        person = Person(external_id='ext-req2', description='Frank')
        session.add(person)
        await session.flush()

        attr = PersonAttribute(person_id=person.id, key=None, value='v')  # type: ignore[arg-type]
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()
