# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for NHIService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.employees.models import Employee
from src.inventory.nhi.repository import get_nhi_by_id
from src.inventory.nhi.service import (
    DuplicateNHIAttributeError,
    InvalidApplicationIdError,
    InvalidOwnerEmployeeIdError,
    NHIAttributeNotFoundError,
    NHINotFoundError,
    NHIService,
)
from src.inventory.persons.models import Person
from src.platform.applications.models import Application
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
def service(log_service: LogService) -> NHIService:
    return NHIService(log_service=log_service)


@pytest.mark.asyncio
async def test_create_nhi(service: NHIService, session_factory) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(
            session,
            external_id='svc-1',
            name='N',
            kind='bot',
        )
        await session.commit()
    assert nhi.id is not None


@pytest.mark.asyncio
async def test_create_nhi_invalid_owner_raises(
    service: NHIService,
    session_factory,
) -> None:
    with pytest.raises(InvalidOwnerEmployeeIdError):
        async with session_factory() as session:
            await service.create_nhi(
                session,
                external_id='x',
                name='Y',
                kind='bot',
                owner_employee_id=uuid.uuid4(),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_create_nhi_invalid_application_raises(
    service: NHIService,
    session_factory,
) -> None:
    with pytest.raises(InvalidApplicationIdError):
        async with session_factory() as session:
            await service.create_nhi(
                session,
                external_id='x',
                name='Y',
                kind='bot',
                application_id=uuid.uuid4(),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_create_nhi_with_valid_fks(
    service: NHIService,
    session_factory,
) -> None:
    async with session_factory() as session:
        person = Person(external_id='p-svc-fk', description='P')
        session.add(person)
        await session.flush()
        employee = Employee(person_id=person.id, is_locked=False)
        session.add(employee)
        await session.flush()
        app = Application(name='a-svc', code='a-svc', config={})
        session.add(app)
        await session.flush()
        nhi = await service.create_nhi(
            session,
            external_id='fk-ok',
            name='Z',
            kind='bot',
            owner_employee_id=employee.id,
            application_id=app.id,
        )
        await session.commit()
    assert nhi.owner_employee_id == employee.id


@pytest.mark.asyncio
async def test_get_nhi(service: NHIService, session_factory) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='g-1', name='G', kind='bot')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        loaded = await service.get_nhi(session, nid)
    assert loaded is not None
    assert loaded.id == nid


@pytest.mark.asyncio
async def test_list_nhi(service: NHIService, session_factory) -> None:
    async with session_factory() as session:
        await service.create_nhi(session, external_id='l-a', name='A', kind='bot')
        await service.create_nhi(session, external_id='l-b', name='B', kind='bot')
        await session.commit()

    async with session_factory() as session:
        rows = await service.list_nhi(session)
    assert len(rows) >= 2


@pytest.mark.asyncio
async def test_list_attributes(service: NHIService, session_factory) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='la-1', name='A', kind='bot')
        await session.flush()
        await service.add_attribute(session, nhi.id, 'k1', 'v1')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        attrs = await service.list_attributes(session, nid)
    assert len(attrs) == 1


@pytest.mark.asyncio
async def test_list_attributes_missing_nhi(
    service: NHIService,
    session_factory,
) -> None:
    with pytest.raises(NHINotFoundError):
        async with session_factory() as session:
            await service.list_attributes(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_add_attribute_duplicate(
    service: NHIService,
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='dup-s', name='D', kind='bot')
        await session.flush()
        await service.add_attribute(session, nhi.id, 'same', 'v1')
        await session.commit()
        nid = nhi.id

    with pytest.raises(DuplicateNHIAttributeError):
        async with session_factory() as session:
            n = await get_nhi_by_id(session, nid)
            assert n is not None
            await service.add_attribute(session, n.id, 'same', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_remove_attribute(service: NHIService, session_factory) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='rm-s', name='R', kind='bot')
        await session.flush()
        await service.add_attribute(session, nhi.id, 'delk', 'x')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        await service.remove_attribute(session, nid, 'delk')
        await session.commit()

    async with session_factory() as session:
        attrs = await service.list_attributes(session, nid)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_remove_attribute_missing(
    service: NHIService,
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='rm-m', name='R', kind='bot')
        await session.commit()
        nid = nhi.id

    with pytest.raises(NHIAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, nid, 'none')
            await session.commit()


@pytest.mark.asyncio
async def test_log_create(
    service: NHIService,
    session_factory,
    log_path: Path,
) -> None:
    async with session_factory() as session:
        await service.create_nhi(session, external_id='log-c', name='L', kind='bot')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'nhi.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'identity-core'


@pytest.mark.asyncio
async def test_log_retrieve(
    service: NHIService,
    session_factory,
    log_path: Path,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='log-r', name='L', kind='bot')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        await service.get_nhi(session, nid)

    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    retrieved = [r for r in records if r.get('event_type') == 'nhi.retrieved']
    assert len(retrieved) >= 1


@pytest.mark.asyncio
async def test_log_add_attribute(
    service: NHIService,
    session_factory,
    log_path: Path,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='log-a', name='L', kind='bot')
        await session.flush()
        await service.add_attribute(session, nhi.id, 'lk', 'lv')
        await session.commit()

    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    added = [r for r in records if r.get('event_type') == 'nhi.attribute.added']
    assert len(added) >= 1


@pytest.mark.asyncio
async def test_log_remove_attribute(
    service: NHIService,
    session_factory,
    log_path: Path,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='log-rm', name='L', kind='bot')
        await session.flush()
        await service.add_attribute(session, nhi.id, 'rk', 'rv')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        await service.remove_attribute(session, nid, 'rk')
        await session.commit()

    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    removed = [r for r in records if r.get('event_type') == 'nhi.attribute.removed']
    assert len(removed) >= 1
