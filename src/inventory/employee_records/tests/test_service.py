# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeRecordService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.employee_records.repository import (
    get_employee_record_by_external_id,
)
from src.inventory.employee_records.service import (
    DuplicateEmployeeRecordAttributeError,
    EmployeeRecordAttributeNotFoundError,
    EmployeeRecordNotFoundError,
    EmployeeRecordService,
    InvalidApplicationIdError,
)
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
def service(log_service: LogService) -> EmployeeRecordService:
    return EmployeeRecordService(log_service=log_service)


@pytest.fixture
async def application_id(session_factory):
    """Create an application for employee record tests."""
    async with session_factory() as session:
        app = Application(name='hr-app', code='hr-app')
        session.add(app)
        await session.commit()
        return app.id


@pytest.mark.asyncio
async def test_create_employee_record(
    service: EmployeeRecordService, session_factory, application_id: uuid.UUID
) -> None:
    """create_employee_record creates and returns record."""
    async with session_factory() as session:
        record = await service.create_employee_record(
            session,
            external_id='rec-svc',
            application_id=application_id,
        )
        await session.commit()
    assert record.id is not None
    assert record.external_id == 'rec-svc'
    assert record.application_id == application_id


@pytest.mark.asyncio
async def test_create_employee_record_invalid_application_id_raises(
    service: EmployeeRecordService,
    session_factory,
) -> None:
    """create_employee_record raises InvalidApplicationIdError when application_id not found."""
    with pytest.raises(InvalidApplicationIdError):
        async with session_factory() as session:
            await service.create_employee_record(
                session,
                external_id='rec-bad',
                application_id=uuid.uuid4(),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_employee_record(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """get_employee_record returns record when found."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-get', application_id=application_id)
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        loaded = await service.get_employee_record(session, record_id)
    assert loaded is not None
    assert loaded.id == record_id


@pytest.mark.asyncio
async def test_get_employee_record_returns_none_when_missing(
    service: EmployeeRecordService,
    session_factory,
) -> None:
    """get_employee_record returns None when not found."""
    async with session_factory() as session:
        result = await service.get_employee_record(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_list_employee_records(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """list_employee_records returns all records."""
    async with session_factory() as session:
        await service.create_employee_record(session, external_id='rec-1', application_id=application_id)
        await service.create_employee_record(session, external_id='rec-2', application_id=application_id)
        await session.commit()

    async with session_factory() as session:
        records = await service.list_employee_records(session)
    assert len(records) >= 2


@pytest.mark.asyncio
async def test_list_attributes(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """list_attributes returns attributes for employee record."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-la', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'attr1', 'val1')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        attrs = await service.list_attributes(session, record_id)
    assert len(attrs) == 1
    assert attrs[0].key == 'attr1'
    assert attrs[0].value == 'val1'


@pytest.mark.asyncio
async def test_list_attributes_raises_when_record_missing(
    service: EmployeeRecordService,
    session_factory,
) -> None:
    """list_attributes raises EmployeeRecordNotFoundError when record missing."""
    with pytest.raises(EmployeeRecordNotFoundError):
        async with session_factory() as session:
            await service.list_attributes(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_add_attribute(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """add_attribute adds and returns attribute."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-add', application_id=application_id)
        await session.flush()
        attr = await service.add_attribute(session, record.id, 'newkey', 'newval')
        await session.commit()
    assert attr.id is not None
    assert attr.key == 'newkey'
    assert attr.value == 'newval'


@pytest.mark.asyncio
async def test_add_attribute_duplicate_key_raises(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """add_attribute raises DuplicateEmployeeRecordAttributeError on duplicate key."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-dup', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'same', 'v1')
        await session.commit()

    with pytest.raises(DuplicateEmployeeRecordAttributeError):
        async with session_factory() as session:
            rec = await get_employee_record_by_external_id(session, 'rec-dup', application_id)
            assert rec is not None
            await service.add_attribute(session, rec.id, 'same', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_remove_attribute(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """remove_attribute removes attribute."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-rm', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'todel', 'x')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        await service.remove_attribute(session, record_id, 'todel')
        await session.commit()

    async with session_factory() as session:
        attrs = await service.list_attributes(session, record_id)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_remove_attribute_raises_when_missing(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """remove_attribute raises EmployeeRecordAttributeNotFoundError when attribute missing."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-norm', application_id=application_id)
        await session.commit()
        record_id = record.id

    with pytest.raises(EmployeeRecordAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, record_id, 'nonexistent')
            await session.commit()


@pytest.mark.asyncio
async def test_log_emission_on_create(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
    log_path: Path,
) -> None:
    """create_employee_record emits employee_record.created log event."""
    async with session_factory() as session:
        await service.create_employee_record(
            session,
            external_id='rec-log',
            application_id=application_id,
        )
        await session.commit()

    assert log_path.exists()
    lines = log_path.read_text().strip().split('\n')
    assert len(lines) >= 1
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'employee_record.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'identity-core'
    assert created[-1]['payload']['external_id'] == 'rec-log'


@pytest.mark.asyncio
async def test_log_emission_on_retrieve(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
    log_path: Path,
) -> None:
    """get_employee_record emits employee_record.retrieved when found."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-ret', application_id=application_id)
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        await service.get_employee_record(session, record_id)

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'employee_record.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'identity-core'


@pytest.mark.asyncio
async def test_log_emission_on_add_attribute(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
    log_path: Path,
) -> None:
    """add_attribute emits employee_record.attribute.added."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-addlog', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'k1', 'v1')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    added = [r for r in records if r.get('event_type') == 'employee_record.attribute.added']
    assert len(added) >= 1
    assert added[-1]['payload']['key'] == 'k1'


@pytest.mark.asyncio
async def test_log_emission_on_remove_attribute(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
    log_path: Path,
) -> None:
    """remove_attribute emits employee_record.attribute.removed."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-rmlog', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'key_to_remove', 'x')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        await service.remove_attribute(session, record_id, 'key_to_remove')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    removed = [r for r in records if r.get('event_type') == 'employee_record.attribute.removed']
    assert len(removed) >= 1
    assert removed[-1]['payload']['key'] == 'key_to_remove'
