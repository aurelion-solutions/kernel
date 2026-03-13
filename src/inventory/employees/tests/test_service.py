# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.employees.repository import get_employee_by_id
from src.inventory.employees.service import (
    DuplicateEmployeeAttributeError,
    EmployeeAttributeNotFoundError,
    EmployeeNotFoundError,
    EmployeeService,
    InvalidPersonIdError,
)
from src.inventory.persons.repository import create_person
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / 'logs.jsonl'


@pytest.fixture
def log_service(log_path: Path) -> LogService:
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    return LogService(factory=factory, provider_name='file')


@pytest.fixture
def service(log_service: LogService) -> EmployeeService:
    return EmployeeService(log_service=log_service)


@pytest.mark.asyncio
async def test_create_employee(service: EmployeeService, session_factory) -> None:
    """create_employee creates and returns employee."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-svc', description='Svc')
        await session.flush()
        employee = await service.create_employee(
            session,
            person_id=person.id,
        )
        await session.commit()
    assert employee.id is not None
    assert employee.person_id == person.id


@pytest.mark.asyncio
async def test_create_employee_invalid_person_id_raises(
    service: EmployeeService,
    session_factory,
) -> None:
    """create_employee raises InvalidPersonIdError when person_id not found."""
    with pytest.raises(InvalidPersonIdError):
        async with session_factory() as session:
            await service.create_employee(
                session,
                person_id=uuid.uuid4(),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_employee(service: EmployeeService, session_factory) -> None:
    """get_employee returns employee when found."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-get', description='Get')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        loaded = await service.get_employee(session, employee_id)
    assert loaded is not None
    assert loaded.id == employee_id


@pytest.mark.asyncio
async def test_get_employee_returns_none_when_missing(
    service: EmployeeService,
    session_factory,
) -> None:
    """get_employee returns None when not found."""
    async with session_factory() as session:
        result = await service.get_employee(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_list_employees(service: EmployeeService, session_factory) -> None:
    """list_employees returns all employees."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-list', description='L')
        await session.flush()
        await service.create_employee(session, person_id=person.id)
        await service.create_employee(session, person_id=person.id)
        await session.commit()

    async with session_factory() as session:
        employees = await service.list_employees(session)
    assert len(employees) >= 2


@pytest.mark.asyncio
async def test_list_attributes(service: EmployeeService, session_factory) -> None:
    """list_attributes returns attributes for employee."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-la', description='LA')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        await service.add_attribute(session, employee.id, 'attr1', 'val1')
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        attrs = await service.list_attributes(session, employee_id)
    assert len(attrs) == 1
    assert attrs[0].key == 'attr1'
    assert attrs[0].value == 'val1'


@pytest.mark.asyncio
async def test_list_attributes_raises_when_employee_missing(
    service: EmployeeService,
    session_factory,
) -> None:
    """list_attributes raises EmployeeNotFoundError when employee missing."""
    with pytest.raises(EmployeeNotFoundError):
        async with session_factory() as session:
            await service.list_attributes(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_add_attribute(service: EmployeeService, session_factory) -> None:
    """add_attribute adds and returns attribute."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-add', description='Add')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        attr = await service.add_attribute(session, employee.id, 'newkey', 'newval')
        await session.commit()
    assert attr.id is not None
    assert attr.key == 'newkey'
    assert attr.value == 'newval'


@pytest.mark.asyncio
async def test_add_attribute_duplicate_key_raises(
    service: EmployeeService,
    session_factory,
) -> None:
    """add_attribute raises DuplicateEmployeeAttributeError on duplicate key."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-dup', description='Dup')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        await service.add_attribute(session, employee.id, 'same', 'v1')
        await session.commit()
        employee_id = employee.id

    with pytest.raises(DuplicateEmployeeAttributeError):
        async with session_factory() as session:
            emp = await get_employee_by_id(session, employee_id)
            assert emp is not None
            await service.add_attribute(session, emp.id, 'same', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_remove_attribute(service: EmployeeService, session_factory) -> None:
    """remove_attribute removes attribute."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-rm', description='Rm')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        await service.add_attribute(session, employee.id, 'todel', 'x')
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        await service.remove_attribute(session, employee_id, 'todel')
        await session.commit()

    async with session_factory() as session:
        attrs = await service.list_attributes(session, employee_id)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_remove_attribute_raises_when_missing(
    service: EmployeeService,
    session_factory,
) -> None:
    """remove_attribute raises EmployeeAttributeNotFoundError when attribute missing."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-norm', description='No')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.commit()
        employee_id = employee.id

    with pytest.raises(EmployeeAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, employee_id, 'nonexistent')
            await session.commit()


@pytest.mark.asyncio
async def test_log_emission_on_create(
    service: EmployeeService,
    session_factory,
    log_path: Path,
) -> None:
    """create_employee emits employee.created log event."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-log', description='Log')
        await session.flush()
        await service.create_employee(
            session,
            person_id=person.id,
        )
        await session.commit()

    assert log_path.exists()
    lines = log_path.read_text().strip().split('\n')
    assert len(lines) >= 1
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'employee.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'identity-core'
    assert 'employee_id' in created[-1]['payload']


@pytest.mark.asyncio
async def test_log_emission_on_retrieve(
    service: EmployeeService,
    session_factory,
    log_path: Path,
) -> None:
    """get_employee emits employee.retrieved when found."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-ret', description='Ret')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        await service.get_employee(session, employee_id)

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'employee.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'identity-core'


@pytest.mark.asyncio
async def test_log_emission_on_add_attribute(
    service: EmployeeService,
    session_factory,
    log_path: Path,
) -> None:
    """add_attribute emits employee.attribute.added."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-addlog', description='Add')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        await service.add_attribute(session, employee.id, 'k1', 'v1')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    added = [r for r in records if r.get('event_type') == 'employee.attribute.added']
    assert len(added) >= 1
    assert added[-1]['payload']['key'] == 'k1'


@pytest.mark.asyncio
async def test_log_emission_on_remove_attribute(
    service: EmployeeService,
    session_factory,
    log_path: Path,
) -> None:
    """remove_attribute emits employee.attribute.removed."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-rmlog', description='Rm')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        await service.add_attribute(session, employee.id, 'key_to_remove', 'x')
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        await service.remove_attribute(session, employee_id, 'key_to_remove')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    removed = [r for r in records if r.get('event_type') == 'employee.attribute.removed']
    assert len(removed) >= 1
    assert removed[-1]['payload']['key'] == 'key_to_remove'
