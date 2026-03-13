# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeRecord repository."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.inventory.employee_records.models import EmployeeRecord
from src.inventory.employee_records.repository import (
    create_employee_record,
    create_employee_record_attribute,
    delete_employee_record_attribute,
    get_employee_record_by_external_id,
    get_employee_record_by_id,
    list_employee_record_attributes,
    list_employee_records,
)
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_create_employee_record(session_factory) -> None:
    """create_employee_record persists an employee record."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app')
        session.add(app)
        await session.flush()
        record = await create_employee_record(
            session,
            external_id='rec-1',
            application_id=app.id,
        )
        await session.commit()
    assert record.id is not None
    assert record.external_id == 'rec-1'
    assert record.application_id == app.id


@pytest.mark.asyncio
async def test_get_employee_record_by_id(session_factory) -> None:
    """get_employee_record_by_id returns record when found."""
    async with session_factory() as session:
        app = Application(name='app-2', code='app-2')
        session.add(app)
        await session.flush()
        record = await create_employee_record(session, external_id='rec-2', application_id=app.id)
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        loaded = await get_employee_record_by_id(session, record_id)
    assert loaded is not None
    assert loaded.id == record_id
    assert loaded.external_id == 'rec-2'


@pytest.mark.asyncio
async def test_get_employee_record_by_id_returns_none_when_missing(
    session_factory,
) -> None:
    """get_employee_record_by_id returns None when not found."""
    async with session_factory() as session:
        loaded = await get_employee_record_by_id(session, uuid.uuid4())
    assert loaded is None


@pytest.mark.asyncio
async def test_get_employee_record_by_external_id(session_factory) -> None:
    """get_employee_record_by_external_id returns record when found."""
    async with session_factory() as session:
        app = Application(name='app-3', code='app-3')
        session.add(app)
        await session.flush()
        await create_employee_record(session, external_id='rec-unique', application_id=app.id)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await get_employee_record_by_external_id(session, 'rec-unique', app_id)
    assert loaded is not None
    assert loaded.external_id == 'rec-unique'


@pytest.mark.asyncio
async def test_list_employee_records(session_factory) -> None:
    """list_employee_records returns all records."""
    async with session_factory() as session:
        app = Application(name='app-4', code='app-4')
        session.add(app)
        await session.flush()
        await create_employee_record(session, external_id='rec-a', application_id=app.id)
        await create_employee_record(session, external_id='rec-b', application_id=app.id)
        await session.commit()

    async with session_factory() as session:
        records = await list_employee_records(session)
    assert len(records) >= 2
    external_ids = [r.external_id for r in records]
    assert 'rec-a' in external_ids
    assert 'rec-b' in external_ids


@pytest.mark.asyncio
async def test_add_attribute(session_factory) -> None:
    """create_employee_record_attribute persists an attribute."""
    async with session_factory() as session:
        app = Application(name='app-5', code='app-5')
        session.add(app)
        await session.flush()
        record = await create_employee_record(session, external_id='rec-attr', application_id=app.id)
        await session.flush()
        attr = await create_employee_record_attribute(
            session,
            employee_record_id=record.id,
            key='dept',
            value='Sales',
        )
        await session.commit()
    assert attr.id is not None
    assert attr.employee_record_id == record.id
    assert attr.key == 'dept'
    assert attr.value == 'Sales'


@pytest.mark.asyncio
async def test_list_attributes(session_factory) -> None:
    """list_employee_record_attributes returns attributes for record."""
    async with session_factory() as session:
        app = Application(name='app-6', code='app-6')
        session.add(app)
        await session.flush()
        record = await create_employee_record(session, external_id='rec-list', application_id=app.id)
        await session.flush()
        await create_employee_record_attribute(session, employee_record_id=record.id, key='k1', value='v1')
        await create_employee_record_attribute(session, employee_record_id=record.id, key='k2', value='v2')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        attrs = await list_employee_record_attributes(session, record_id)
    assert len(attrs) == 2
    keys = {a.key for a in attrs}
    assert keys == {'k1', 'k2'}


@pytest.mark.asyncio
async def test_delete_attribute(session_factory) -> None:
    """delete_employee_record_attribute removes attribute."""
    async with session_factory() as session:
        app = Application(name='app-7', code='app-7')
        session.add(app)
        await session.flush()
        record = await create_employee_record(session, external_id='rec-del', application_id=app.id)
        await session.flush()
        await create_employee_record_attribute(session, employee_record_id=record.id, key='to_del', value='x')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        deleted = await delete_employee_record_attribute(session, record_id, 'to_del')
        await session.commit()
    assert deleted is True

    async with session_factory() as session:
        attrs = await list_employee_record_attributes(session, record_id)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_delete_attribute_nonexistent_returns_false(session_factory) -> None:
    """delete_employee_record_attribute returns False when attribute not found."""
    async with session_factory() as session:
        app = Application(name='app-8', code='app-8')
        session.add(app)
        await session.flush()
        record = await create_employee_record(session, external_id='rec-nodel', application_id=app.id)
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        deleted = await delete_employee_record_attribute(session, record_id, 'nonexistent')
    assert deleted is False


@pytest.mark.asyncio
async def test_uniqueness_on_employee_record_id_key_enforced(
    session_factory,
) -> None:
    """Duplicate (employee_record_id, key) is rejected."""
    async with session_factory() as session:
        app = Application(name='app-9', code='app-9')
        session.add(app)
        await session.flush()
        record = await create_employee_record(session, external_id='rec-dup', application_id=app.id)
        await session.flush()
        await create_employee_record_attribute(session, employee_record_id=record.id, key='dup', value='v1')
        await session.commit()

    async with session_factory() as session:
        rec = (
            await session.execute(select(EmployeeRecord).where(EmployeeRecord.external_id == 'rec-dup'))
        ).scalar_one()
        with pytest.raises(IntegrityError):
            await create_employee_record_attribute(session, employee_record_id=rec.id, key='dup', value='v2')


@pytest.mark.asyncio
async def test_invalid_application_id_rejected(session_factory) -> None:
    """create_employee_record with nonexistent application_id raises IntegrityError."""
    async with session_factory() as session:
        fake_app_id = uuid.uuid4()
        with pytest.raises(IntegrityError):
            await create_employee_record(
                session,
                external_id='rec-bad',
                application_id=fake_app_id,
            )
            await session.commit()
