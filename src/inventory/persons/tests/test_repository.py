# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Person repository."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.inventory.persons.models import Person
from src.inventory.persons.repository import (
    create_person,
    create_person_attribute,
    delete_person_attribute,
    get_person_by_external_id,
    get_person_by_id,
    list_person_attributes,
    list_persons_page,
)


@pytest.mark.asyncio
async def test_create_person(session_factory) -> None:
    """create_person persists a person."""
    async with session_factory() as session:
        person = await create_person(
            session,
            external_id='ext-1',
            full_name='Alice',
        )
        await session.commit()
    assert person.id is not None
    assert person.external_id == 'ext-1'
    assert person.full_name == 'Alice'


@pytest.mark.asyncio
async def test_get_person_by_id(session_factory) -> None:
    """get_person_by_id returns person when found."""
    async with session_factory() as session:
        person = await create_person(session, external_id='ext-2', full_name='Bob')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        loaded = await get_person_by_id(session, person_id)
    assert loaded is not None
    assert loaded.id == person_id
    assert loaded.external_id == 'ext-2'


@pytest.mark.asyncio
async def test_get_person_by_id_returns_none_when_missing(session_factory) -> None:
    """get_person_by_id returns None when not found."""
    async with session_factory() as session:
        loaded = await get_person_by_id(session, uuid.uuid4())
    assert loaded is None


@pytest.mark.asyncio
async def test_get_person_by_external_id(session_factory) -> None:
    """get_person_by_external_id returns person when found."""
    async with session_factory() as session:
        await create_person(session, external_id='ext-unique', full_name='Carol')
        await session.commit()

    async with session_factory() as session:
        loaded = await get_person_by_external_id(session, 'ext-unique')
    assert loaded is not None
    assert loaded.external_id == 'ext-unique'


@pytest.mark.asyncio
async def test_list_persons_page_happy(session_factory) -> None:
    """list_persons_page returns rows and total for a normal page."""
    async with session_factory() as session:
        await create_person(session, external_id='ext-page-a', full_name='PageA')
        await create_person(session, external_id='ext-page-b', full_name='PageB')
        await session.commit()

    async with session_factory() as session:
        rows, total = await list_persons_page(session, limit=100, offset=0)
    assert len(rows) >= 2
    assert total >= 2
    external_ids = [p.external_id for p in rows]
    assert 'ext-page-a' in external_ids
    assert 'ext-page-b' in external_ids


@pytest.mark.asyncio
async def test_list_persons_page_past_the_end(session_factory) -> None:
    """list_persons_page with offset beyond total returns empty rows but correct total."""
    async with session_factory() as session:
        await create_person(session, external_id='ext-pp-1', full_name='PastPagePerson')
        await session.commit()

    async with session_factory() as session:
        rows, total = await list_persons_page(session, limit=10, offset=9999)
    assert rows == []
    assert total >= 1


@pytest.mark.asyncio
async def test_add_attribute(session_factory) -> None:
    """create_person_attribute persists an attribute."""
    async with session_factory() as session:
        person = await create_person(session, external_id='ext-attr', full_name='Dave')
        await session.flush()
        attr = await create_person_attribute(
            session,
            person_id=person.id,
            key='dept',
            value='Sales',
        )
        await session.commit()
    assert attr.id is not None
    assert attr.person_id == person.id
    assert attr.key == 'dept'
    assert attr.value == 'Sales'


@pytest.mark.asyncio
async def test_list_attributes(session_factory) -> None:
    """list_person_attributes returns attributes for person."""
    async with session_factory() as session:
        person = await create_person(session, external_id='ext-list', full_name='Eve')
        await session.flush()
        await create_person_attribute(session, person_id=person.id, key='k1', value='v1')
        await create_person_attribute(session, person_id=person.id, key='k2', value='v2')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        attrs = await list_person_attributes(session, person_id)
    assert len(attrs) == 2
    keys = {a.key for a in attrs}
    assert keys == {'k1', 'k2'}


@pytest.mark.asyncio
async def test_delete_attribute(session_factory) -> None:
    """delete_person_attribute removes attribute."""
    async with session_factory() as session:
        person = await create_person(session, external_id='ext-del', full_name='Frank')
        await session.flush()
        await create_person_attribute(session, person_id=person.id, key='to_del', value='x')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        deleted = await delete_person_attribute(session, person_id, 'to_del')
        await session.commit()
    assert deleted is True

    async with session_factory() as session:
        attrs = await list_person_attributes(session, person_id)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_delete_attribute_nonexistent_returns_false(session_factory) -> None:
    """delete_person_attribute returns False when attribute not found."""
    async with session_factory() as session:
        person = await create_person(session, external_id='ext-nodel', full_name='Gina')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        deleted = await delete_person_attribute(session, person_id, 'nonexistent')
    assert deleted is False


@pytest.mark.asyncio
async def test_uniqueness_on_person_id_key_enforced(session_factory) -> None:
    """Duplicate (person_id, key) is rejected."""
    async with session_factory() as session:
        person = await create_person(session, external_id='ext-dup', full_name='Hank')
        await session.flush()
        await create_person_attribute(session, person_id=person.id, key='dup', value='v1')
        await session.commit()

    async with session_factory() as session:
        person = (await session.execute(select(Person).where(Person.external_id == 'ext-dup'))).scalar_one()
        with pytest.raises(IntegrityError):
            await create_person_attribute(session, person_id=person.id, key='dup', value='v2')
