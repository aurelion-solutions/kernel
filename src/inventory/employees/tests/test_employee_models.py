# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Employee and EmployeeAttribute models."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.persons.models import Person


@pytest.mark.asyncio
async def test_create_employee_with_required_fields(session_factory) -> None:
    """Employee can be created with required fields."""
    async with session_factory() as session:
        person = Person(external_id='ext-emp1', full_name='Alice')
        session.add(person)
        await session.flush()

        employee = Employee(
            person_id=person.id,
            is_locked=False,
        )
        session.add(employee)
        await session.flush()
        assert employee.id is not None
        assert employee.person_id == person.id
        assert employee.is_locked is False
        assert employee.description is None


@pytest.mark.asyncio
async def test_create_employee_linked_to_existing_person(session_factory) -> None:
    """Employee can be created linked to existing Person."""
    async with session_factory() as session:
        person = Person(external_id='ext-emp2', full_name='Bob')
        session.add(person)
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        employee = Employee(
            person_id=person_id,
            is_locked=True,
            description='Bob the employee',
        )
        session.add(employee)
        await session.commit()
        assert employee.person_id == person_id
        assert employee.description == 'Bob the employee'


@pytest.mark.asyncio
async def test_create_employee_attribute_linked_to_employee(session_factory) -> None:
    """EmployeeAttribute can be created linked to Employee."""
    async with session_factory() as session:
        person = Person(external_id='ext-ea', full_name='Carol')
        session.add(person)
        await session.flush()
        employee = Employee(
            person_id=person.id,
            is_locked=False,
        )
        session.add(employee)
        await session.flush()

        attr = EmployeeAttribute(
            employee_id=employee.id,
            key='department',
            value='Engineering',
        )
        session.add(attr)
        await session.flush()
        assert attr.id is not None
        assert attr.employee_id == employee.id
        assert attr.key == 'department'
        assert attr.value == 'Engineering'


@pytest.mark.asyncio
async def test_employee_belongs_to_person(session_factory) -> None:
    """Employee belongs to Person; relationship works."""
    async with session_factory() as session:
        person = Person(external_id='ext-rel', full_name='Dave')
        session.add(person)
        await session.flush()
        employee = Employee(
            person_id=person.id,
            is_locked=False,
        )
        session.add(employee)
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        result = await session.execute(
            select(Employee).where(Employee.id == employee_id).options(selectinload(Employee.person))
        )
        loaded = result.scalar_one()
        assert loaded is not None
        assert loaded.person is not None
        assert loaded.person.external_id == 'ext-rel'


@pytest.mark.asyncio
async def test_employee_attribute_belongs_to_employee(session_factory) -> None:
    """EmployeeAttribute belongs to Employee; relationship works both ways."""
    async with session_factory() as session:
        person = Person(external_id='ext-ea2', full_name='Eve')
        session.add(person)
        await session.flush()
        employee = Employee(
            person_id=person.id,
            is_locked=False,
        )
        session.add(employee)
        await session.flush()
        attr = EmployeeAttribute(
            employee_id=employee.id,
            key='title',
            value='Engineer',
        )
        session.add(attr)
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        result = await session.execute(
            select(Employee).where(Employee.id == employee_id).options(selectinload(Employee.attributes))
        )
        loaded = result.scalar_one()
        assert loaded is not None
        assert len(loaded.attributes) == 1
        assert loaded.attributes[0].key == 'title'
        assert loaded.attributes[0].employee is loaded


@pytest.mark.asyncio
async def test_uniqueness_on_employee_id_key_enforced(session_factory) -> None:
    """Duplicate (employee_id, key) pair is rejected."""
    async with session_factory() as session:
        person = Person(external_id='ext-dup', full_name='Frank')
        session.add(person)
        await session.flush()
        employee = Employee(
            person_id=person.id,
            is_locked=False,
        )
        session.add(employee)
        await session.flush()
        attr1 = EmployeeAttribute(
            employee_id=employee.id,
            key='email',
            value='f@example.com',
        )
        session.add(attr1)
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        employee = (await session.execute(select(Employee).where(Employee.id == employee_id))).scalar_one()
        attr2 = EmployeeAttribute(
            employee_id=employee.id,
            key='email',
            value='other@example.com',
        )
        session.add(attr2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_employee_attribute_missing_value(
    session_factory,
) -> None:
    """EmployeeAttribute requires value; missing raises IntegrityError."""
    async with session_factory() as session:
        person = Person(external_id='ext-eareq', full_name='Hank')
        session.add(person)
        await session.flush()
        employee = Employee(
            person_id=person.id,
            is_locked=False,
        )
        session.add(employee)
        await session.flush()

        attr = EmployeeAttribute(
            employee_id=employee.id,
            key='k',
            value=None,  # type: ignore[arg-type]
        )
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_employee_attribute_missing_key(
    session_factory,
) -> None:
    """EmployeeAttribute requires key; missing raises IntegrityError."""
    async with session_factory() as session:
        person = Person(external_id='ext-eareq2', full_name='Ivan')
        session.add(person)
        await session.flush()
        employee = Employee(
            person_id=person.id,
            is_locked=False,
        )
        session.add(employee)
        await session.flush()

        attr = EmployeeAttribute(
            employee_id=employee.id,
            key=None,  # type: ignore[arg-type]
            value='v',
        )
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_invalid_person_id_rejected(session_factory) -> None:
    """Invalid person_id (non-existent) is rejected by FK constraint."""
    async with session_factory() as session:
        employee = Employee(
            person_id=uuid.uuid4(),  # non-existent person
            is_locked=False,
        )
        session.add(employee)
        with pytest.raises(IntegrityError):
            await session.commit()
