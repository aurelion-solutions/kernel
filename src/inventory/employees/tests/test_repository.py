# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Employee repository."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.inventory.employees.models import Employee
from src.inventory.employees.repository import (
    create_employee,
    create_employee_attribute,
    delete_employee_attribute,
    get_employee_by_id,
    list_employee_attributes,
    list_employees,
)
from src.inventory.persons.repository import create_person


@pytest.mark.asyncio
async def test_create_employee(session_factory) -> None:
    """create_employee persists an employee."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-1', description='Alice')
        await session.flush()
        employee = await create_employee(
            session,
            person_id=person.id,
        )
        await session.commit()
    assert employee.id is not None
    assert employee.person_id == person.id


@pytest.mark.asyncio
async def test_get_employee_by_id(session_factory) -> None:
    """get_employee_by_id returns employee when found."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-2', description='Bob')
        await session.flush()
        employee = await create_employee(session, person_id=person.id)
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        loaded = await get_employee_by_id(session, employee_id)
    assert loaded is not None
    assert loaded.id == employee_id


@pytest.mark.asyncio
async def test_get_employee_by_id_returns_none_when_missing(session_factory) -> None:
    """get_employee_by_id returns None when not found."""
    async with session_factory() as session:
        loaded = await get_employee_by_id(session, uuid.uuid4())
    assert loaded is None


@pytest.mark.asyncio
async def test_list_employees(session_factory) -> None:
    """list_employees returns all employees."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-4', description='Dave')
        await session.flush()
        await create_employee(session, person_id=person.id)
        await create_employee(session, person_id=person.id)
        await session.commit()

    async with session_factory() as session:
        employees = await list_employees(session)
    assert len(employees) >= 2


@pytest.mark.asyncio
async def test_add_attribute(session_factory) -> None:
    """create_employee_attribute persists an attribute."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-5', description='Eve')
        await session.flush()
        employee = await create_employee(session, person_id=person.id)
        await session.flush()
        attr = await create_employee_attribute(
            session,
            employee_id=employee.id,
            key='dept',
            value='Sales',
        )
        await session.commit()
    assert attr.id is not None
    assert attr.employee_id == employee.id
    assert attr.key == 'dept'
    assert attr.value == 'Sales'


@pytest.mark.asyncio
async def test_list_attributes(session_factory) -> None:
    """list_employee_attributes returns attributes for employee."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-6', description='Frank')
        await session.flush()
        employee = await create_employee(session, person_id=person.id)
        await session.flush()
        await create_employee_attribute(session, employee_id=employee.id, key='k1', value='v1')
        await create_employee_attribute(session, employee_id=employee.id, key='k2', value='v2')
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        attrs = await list_employee_attributes(session, employee_id)
    assert len(attrs) == 2
    keys = {a.key for a in attrs}
    assert keys == {'k1', 'k2'}


@pytest.mark.asyncio
async def test_delete_attribute(session_factory) -> None:
    """delete_employee_attribute removes attribute."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-7', description='Gina')
        await session.flush()
        employee = await create_employee(session, person_id=person.id)
        await session.flush()
        await create_employee_attribute(session, employee_id=employee.id, key='to_del', value='x')
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        deleted = await delete_employee_attribute(session, employee_id, 'to_del')
        await session.commit()
    assert deleted is True

    async with session_factory() as session:
        attrs = await list_employee_attributes(session, employee_id)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_delete_attribute_nonexistent_returns_false(session_factory) -> None:
    """delete_employee_attribute returns False when attribute not found."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-8', description='Hank')
        await session.flush()
        employee = await create_employee(session, person_id=person.id)
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        deleted = await delete_employee_attribute(session, employee_id, 'nonexistent')
    assert deleted is False


@pytest.mark.asyncio
async def test_uniqueness_on_employee_id_key_enforced(session_factory) -> None:
    """Duplicate (employee_id, key) is rejected."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-9', description='Ivan')
        await session.flush()
        employee = await create_employee(session, person_id=person.id)
        await session.flush()
        await create_employee_attribute(session, employee_id=employee.id, key='dup', value='v1')
        await session.commit()

    async with session_factory() as session:
        emp = (await session.execute(select(Employee).where(Employee.id == employee.id))).scalar_one()
        with pytest.raises(IntegrityError):
            await create_employee_attribute(session, employee_id=emp.id, key='dup', value='v2')


@pytest.mark.asyncio
async def test_invalid_person_id_rejected(session_factory) -> None:
    """create_employee with nonexistent person_id raises IntegrityError."""
    async with session_factory() as session:
        fake_person_id = uuid.uuid4()
        with pytest.raises(IntegrityError):
            await create_employee(
                session,
                person_id=fake_person_id,
            )
            await session.commit()
