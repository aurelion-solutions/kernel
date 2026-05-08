# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeRecord and EmployeeRecordAttribute models."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from src.inventory.employee_records.models import (
    EmployeeProviderAttributeMapping,
    EmployeeRecord,
    EmployeeRecordAttribute,
    EmployeeRecordMatch,
)
from src.inventory.employees.models import Employee
from src.inventory.persons.models import Person
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_create_employee_record_with_required_fields(
    session_factory,
) -> None:
    """EmployeeRecord can be created with required fields."""
    async with session_factory() as session:
        app = Application(name='test-app', code='test-app')
        session.add(app)
        await session.flush()

        record = EmployeeRecord(
            external_id='rec-001',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()
        assert record.id is not None
        assert record.external_id == 'rec-001'
        assert record.application_id == app.id
        assert record.description is None


@pytest.mark.asyncio
async def test_create_employee_record_linked_to_existing_application(
    session_factory,
) -> None:
    """EmployeeRecord can be created linked to existing Application."""
    async with session_factory() as session:
        app = Application(name='hr-app', code='hr-app')
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        record = EmployeeRecord(
            external_id='rec-002',
            application_id=app_id,
            description='John from HRIS',
        )
        session.add(record)
        await session.commit()
        assert record.application_id == app_id
        assert record.description == 'John from HRIS'


@pytest.mark.asyncio
async def test_create_employee_record_attribute_linked_to_employee_record(
    session_factory,
) -> None:
    """EmployeeRecordAttribute can be created linked to EmployeeRecord."""
    async with session_factory() as session:
        app = Application(name='attr-app', code='attr-app')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-attr',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()

        attr = EmployeeRecordAttribute(
            employee_record_id=record.id,
            key='department',
            value='Engineering',
        )
        session.add(attr)
        await session.flush()
        assert attr.id is not None
        assert attr.employee_record_id == record.id
        assert attr.key == 'department'
        assert attr.value == 'Engineering'


@pytest.mark.asyncio
async def test_employee_record_belongs_to_application(session_factory) -> None:
    """EmployeeRecord belongs to Application; relationship works."""
    async with session_factory() as session:
        app = Application(name='rel-app', code='rel-app')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-rel',
            application_id=app.id,
        )
        session.add(record)
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        result = await session.execute(
            select(EmployeeRecord)
            .where(EmployeeRecord.id == record_id)
            .options(selectinload(EmployeeRecord.application))
        )
        loaded = result.scalar_one()
        assert loaded is not None
        assert loaded.application is not None
        assert loaded.application.name == 'rel-app'


@pytest.mark.asyncio
async def test_employee_record_attribute_belongs_to_employee_record(
    session_factory,
) -> None:
    """EmployeeRecordAttribute belongs to EmployeeRecord; relationship works."""
    async with session_factory() as session:
        app = Application(name='attr-rel-app', code='attr-rel-app')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-attr-rel',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()
        attr = EmployeeRecordAttribute(
            employee_record_id=record.id,
            key='title',
            value='Engineer',
        )
        session.add(attr)
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        result = await session.execute(
            select(EmployeeRecord)
            .where(EmployeeRecord.id == record_id)
            .options(selectinload(EmployeeRecord.attributes))
        )
        loaded = result.scalar_one()
        assert loaded is not None
        assert len(loaded.attributes) == 1
        assert loaded.attributes[0].key == 'title'
        assert loaded.attributes[0].employee_record is loaded


@pytest.mark.asyncio
async def test_uniqueness_on_employee_record_id_key_enforced(
    session_factory,
) -> None:
    """Duplicate (employee_record_id, key) is rejected."""
    async with session_factory() as session:
        app = Application(name='dup-app', code='dup-app')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-dup',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()
        attr1 = EmployeeRecordAttribute(
            employee_record_id=record.id,
            key='email',
            value='a@example.com',
        )
        session.add(attr1)
        await session.commit()

    async with session_factory() as session:
        rec = (
            await session.execute(select(EmployeeRecord).where(EmployeeRecord.external_id == 'rec-dup'))
        ).scalar_one()
        attr2 = EmployeeRecordAttribute(
            employee_record_id=rec.id,
            key='email',
            value='b@example.com',
        )
        session.add(attr2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_employee_record_missing_external_id(
    session_factory,
) -> None:
    """EmployeeRecord requires external_id; missing raises IntegrityError."""
    async with session_factory() as session:
        app = Application(name='req-app', code='req-app')
        session.add(app)
        await session.flush()

        record = EmployeeRecord(
            external_id=None,  # type: ignore[arg-type]
            application_id=app.id,
        )
        session.add(record)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_employee_record_attribute_missing_value(
    session_factory,
) -> None:
    """EmployeeRecordAttribute requires value; missing raises IntegrityError."""
    async with session_factory() as session:
        app = Application(name='req-attr-app', code='req-attr-app')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-reqattr',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()

        attr = EmployeeRecordAttribute(
            employee_record_id=record.id,
            key='k',
            value=None,  # type: ignore[arg-type]
        )
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_employee_record_attribute_missing_key(
    session_factory,
) -> None:
    """EmployeeRecordAttribute requires key; missing raises IntegrityError."""
    async with session_factory() as session:
        app = Application(name='req-key-app', code='req-key-app')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-reqkey',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()

        attr = EmployeeRecordAttribute(
            employee_record_id=record.id,
            key=None,  # type: ignore[arg-type]
            value='v',
        )
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_invalid_application_id_rejected(session_factory) -> None:
    """Invalid application_id (non-existent) is rejected by FK constraint."""
    async with session_factory() as session:
        record = EmployeeRecord(
            external_id='rec-bad-app',
            application_id=uuid.uuid4(),  # non-existent application
        )
        session.add(record)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_create_employee_provider_attribute_mapping(session_factory) -> None:
    """EmployeeProviderAttributeMapping can be created."""
    async with session_factory() as session:
        app = Application(name='map-app', code='map-app')
        session.add(app)
        await session.flush()
        row = EmployeeProviderAttributeMapping(
            application_id=app.id,
            employee_record_key='src_key',
            employee_key='canon_key',
            is_determinator=True,
            allow_upstream=False,
        )
        session.add(row)
        await session.commit()
        assert row.id is not None


@pytest.mark.asyncio
async def test_uniqueness_on_application_id_employee_record_key(
    session_factory,
) -> None:
    """Duplicate (application_id, employee_record_key) is rejected."""
    async with session_factory() as session:
        app = Application(name='map-dup-app', code='map-dup-app')
        session.add(app)
        await session.flush()
        m1 = EmployeeProviderAttributeMapping(
            application_id=app.id,
            employee_record_key='same_key',
            employee_key='k1',
            is_determinator=True,
            allow_upstream=False,
        )
        session.add(m1)
        await session.commit()

    async with session_factory() as session:
        app = (await session.execute(select(Application).where(Application.name == 'map-dup-app'))).scalar_one()
        m2 = EmployeeProviderAttributeMapping(
            application_id=app.id,
            employee_record_key='same_key',
            employee_key='k2',
            is_determinator=False,
            allow_upstream=True,
        )
        session.add(m2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_create_employee_record_match(session_factory) -> None:
    """EmployeeRecordMatch links EmployeeRecord to Employee."""
    async with session_factory() as session:
        app = Application(name='match-app', code='match-app')
        session.add(app)
        await session.flush()
        person = Person(external_id='p-match', full_name='P')
        session.add(person)
        await session.flush()
        employee = Employee(person_id=person.id)
        session.add(employee)
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-match',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()
        match = EmployeeRecordMatch(
            employee_record_id=record.id,
            employee_id=employee.id,
            matched_via_determinator=True,
        )
        session.add(match)
        await session.commit()
        assert match.id is not None


@pytest.mark.asyncio
async def test_one_employee_record_cannot_have_multiple_matches(
    session_factory,
) -> None:
    """Duplicate employee_record_id on EmployeeRecordMatch is rejected."""
    async with session_factory() as session:
        app = Application(name='match-dup-app', code='match-dup-app')
        session.add(app)
        await session.flush()
        person1 = Person(external_id='p-md-1', full_name='P')
        person2 = Person(external_id='p-md-2', full_name='P')
        session.add_all([person1, person2])
        await session.flush()
        e1 = Employee(person_id=person1.id)
        e2 = Employee(person_id=person2.id)
        session.add_all([e1, e2])
        await session.flush()
        record = EmployeeRecord(
            external_id='rec-md',
            application_id=app.id,
        )
        session.add(record)
        await session.flush()
        session.add(
            EmployeeRecordMatch(
                employee_record_id=record.id,
                employee_id=e1.id,
                matched_via_determinator=True,
            )
        )
        await session.commit()

    async with session_factory() as session:
        rec = (await session.execute(select(EmployeeRecord).where(EmployeeRecord.external_id == 'rec-md'))).scalar_one()
        person2 = (await session.execute(select(Person).where(Person.external_id == 'p-md-2'))).scalar_one()
        e2 = (await session.execute(select(Employee).where(Employee.person_id == person2.id))).scalar_one()
        session.add(
            EmployeeRecordMatch(
                employee_record_id=rec.id,
                employee_id=e2.id,
                matched_via_determinator=False,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
