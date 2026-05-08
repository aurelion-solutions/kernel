# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for NHI repository."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import NHI
from src.inventory.nhi.repository import (
    create_nhi,
    create_nhi_attribute,
    delete_nhi_attribute,
    get_nhi_by_external_id,
    get_nhi_by_id,
    list_nhi,
    list_nhi_attributes,
)
from src.inventory.persons.models import Person
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_create_nhi(session_factory) -> None:
    async with session_factory() as session:
        nhi = await create_nhi(
            session,
            external_id='r-1',
            name='N',
            kind='bot',
        )
        await session.commit()
    assert nhi.id is not None
    assert nhi.external_id == 'r-1'


@pytest.mark.asyncio
async def test_get_nhi_by_id(session_factory) -> None:
    async with session_factory() as session:
        nhi = await create_nhi(session, external_id='r-2', name='A', kind='bot')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        loaded = await get_nhi_by_id(session, nid)
    assert loaded is not None
    assert loaded.id == nid


@pytest.mark.asyncio
async def test_get_nhi_by_external_id(session_factory) -> None:
    async with session_factory() as session:
        await create_nhi(session, external_id='uniq-ext', name='B', kind='bot')
        await session.commit()

    async with session_factory() as session:
        loaded = await get_nhi_by_external_id(session, 'uniq-ext')
    assert loaded is not None
    assert loaded.external_id == 'uniq-ext'


@pytest.mark.asyncio
async def test_list_nhi(session_factory) -> None:
    async with session_factory() as session:
        await create_nhi(session, external_id='a-list', name='L1', kind='bot')
        await create_nhi(session, external_id='b-list', name='L2', kind='bot')
        await session.commit()

    async with session_factory() as session:
        rows = await list_nhi(session)
    assert len(rows) >= 2


@pytest.mark.asyncio
async def test_add_and_list_attributes(session_factory) -> None:
    async with session_factory() as session:
        nhi = await create_nhi(session, external_id='attr-r', name='A', kind='bot')
        await session.flush()
        await create_nhi_attribute(session, nhi_id=nhi.id, key='k1', value='v1')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        attrs = await list_nhi_attributes(session, nid)
    assert len(attrs) == 1
    assert attrs[0].key == 'k1'


@pytest.mark.asyncio
async def test_delete_attribute(session_factory) -> None:
    async with session_factory() as session:
        nhi = await create_nhi(session, external_id='del-r', name='D', kind='bot')
        await session.flush()
        await create_nhi_attribute(session, nhi_id=nhi.id, key='rm', value='x')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        ok = await delete_nhi_attribute(session, nid, 'rm')
        await session.commit()
    assert ok is True

    async with session_factory() as session:
        attrs = await list_nhi_attributes(session, nid)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_uniqueness_nhi_id_key(session_factory) -> None:
    async with session_factory() as session:
        nhi = await create_nhi(session, external_id='dup-r', name='D', kind='bot')
        await session.flush()
        await create_nhi_attribute(session, nhi_id=nhi.id, key='same', value='v1')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        nhi_row = (await session.execute(select(NHI).where(NHI.id == nid))).scalar_one()
        with pytest.raises(IntegrityError):
            await create_nhi_attribute(session, nhi_id=nhi_row.id, key='same', value='v2')
            await session.commit()


@pytest.mark.asyncio
async def test_invalid_owner_employee_id(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await create_nhi(
                session,
                external_id='bad-emp',
                name='X',
                kind='bot',
                owner_employee_id=uuid.uuid4(),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_invalid_application_id(session_factory) -> None:
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await create_nhi(
                session,
                external_id='bad-app',
                name='X',
                kind='bot',
                application_id=uuid.uuid4(),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_create_nhi_with_valid_fks(session_factory) -> None:
    async with session_factory() as session:
        person = Person(external_id='p-nhi-r', full_name='P')
        session.add(person)
        await session.flush()
        employee = Employee(person_id=person.id, is_locked=False)
        session.add(employee)
        await session.flush()
        app = Application(name='app-nhi-r', code='app-nhi-r', config={})
        session.add(app)
        await session.flush()
        nhi = await create_nhi(
            session,
            external_id='ok-fk',
            name='Y',
            kind='bot',
            owner_employee_id=employee.id,
            application_id=app.id,
        )
        await session.commit()
    assert nhi.owner_employee_id is not None
    assert nhi.application_id is not None
