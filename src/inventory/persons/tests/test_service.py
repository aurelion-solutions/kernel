# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PersonService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.persons.repository import get_person_by_external_id
from src.inventory.persons.service import (
    DuplicatePersonAttributeError,
    PersonAttributeNotFoundError,
    PersonNotFoundError,
    PersonService,
)
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
def service(log_service: LogService) -> PersonService:
    return PersonService(log_service=log_service)


@pytest.mark.asyncio
async def test_create_person(service: PersonService, session_factory) -> None:
    """create_person creates and returns person."""
    async with session_factory() as session:
        person = await service.create_person(
            session,
            external_id='ext-svc',
            description='Test Person',
        )
        await session.commit()
    assert person.id is not None
    assert person.external_id == 'ext-svc'
    assert person.description == 'Test Person'


@pytest.mark.asyncio
async def test_get_person(service: PersonService, session_factory) -> None:
    """get_person returns person when found."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-get', description='Get')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        loaded = await service.get_person(session, person_id)
    assert loaded is not None
    assert loaded.id == person_id


@pytest.mark.asyncio
async def test_get_person_returns_none_when_missing(
    service: PersonService,
    session_factory,
) -> None:
    """get_person returns None when not found."""
    async with session_factory() as session:
        result = await service.get_person(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_list_persons(service: PersonService, session_factory) -> None:
    """list_persons returns all persons."""
    async with session_factory() as session:
        await service.create_person(session, external_id='ext-1', description='One')
        await service.create_person(session, external_id='ext-2', description='Two')
        await session.commit()

    async with session_factory() as session:
        persons = await service.list_persons(session)
    assert len(persons) >= 2


@pytest.mark.asyncio
async def test_list_attributes(service: PersonService, session_factory) -> None:
    """list_attributes returns attributes for person."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-la', description='List')
        await session.flush()
        await service.add_attribute(session, person.id, 'attr1', 'val1')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        attrs = await service.list_attributes(session, person_id)
    assert len(attrs) == 1
    assert attrs[0].key == 'attr1'
    assert attrs[0].value == 'val1'


@pytest.mark.asyncio
async def test_list_attributes_raises_when_person_missing(
    service: PersonService,
    session_factory,
) -> None:
    """list_attributes raises PersonNotFoundError when person missing."""
    with pytest.raises(PersonNotFoundError):
        async with session_factory() as session:
            await service.list_attributes(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_add_attribute(service: PersonService, session_factory) -> None:
    """add_attribute adds and returns attribute."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-add', description='Add')
        await session.flush()
        attr = await service.add_attribute(session, person.id, 'newkey', 'newval')
        await session.commit()
    assert attr.id is not None
    assert attr.key == 'newkey'
    assert attr.value == 'newval'


@pytest.mark.asyncio
async def test_add_attribute_duplicate_key_raises(
    service: PersonService,
    session_factory,
) -> None:
    """add_attribute raises DuplicatePersonAttributeError on duplicate key."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-dup', description='Dup')
        await session.flush()
        await service.add_attribute(session, person.id, 'same', 'v1')
        await session.commit()

    with pytest.raises(DuplicatePersonAttributeError):
        async with session_factory() as session:
            person = await get_person_by_external_id(session, 'ext-dup')
            assert person is not None
            await service.add_attribute(session, person.id, 'same', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_remove_attribute(service: PersonService, session_factory) -> None:
    """remove_attribute removes attribute."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-rm', description='Rm')
        await session.flush()
        await service.add_attribute(session, person.id, 'todel', 'x')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        await service.remove_attribute(session, person_id, 'todel')
        await session.commit()

    async with session_factory() as session:
        attrs = await service.list_attributes(session, person_id)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_remove_attribute_raises_when_missing(
    service: PersonService,
    session_factory,
) -> None:
    """remove_attribute raises PersonAttributeNotFoundError when attribute missing."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-norm', description='No')
        await session.commit()
        person_id = person.id

    with pytest.raises(PersonAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, person_id, 'nonexistent')
            await session.commit()


@pytest.mark.asyncio
async def test_log_emission_on_create(
    service: PersonService,
    session_factory,
    log_path: Path,
) -> None:
    """create_person emits person.created log event."""
    async with session_factory() as session:
        await service.create_person(
            session,
            external_id='ext-log',
            description='Log',
        )
        await session.commit()

    assert log_path.exists()
    lines = log_path.read_text().strip().split('\n')
    assert len(lines) >= 1
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'person.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'identity-core'
    assert created[-1]['payload']['external_id'] == 'ext-log'


@pytest.mark.asyncio
async def test_log_emission_on_retrieve(
    service: PersonService,
    session_factory,
    log_path: Path,
) -> None:
    """get_person emits person.retrieved when found."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-ret', description='Ret')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        await service.get_person(session, person_id)

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'person.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'identity-core'


@pytest.mark.asyncio
async def test_log_emission_on_add_attribute(
    service: PersonService,
    session_factory,
    log_path: Path,
) -> None:
    """add_attribute emits person.attribute.added."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-addlog', description='Add')
        await session.flush()
        await service.add_attribute(session, person.id, 'k1', 'v1')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    added = [r for r in records if r.get('event_type') == 'person.attribute.added']
    assert len(added) >= 1
    assert added[-1]['payload']['key'] == 'k1'


@pytest.mark.asyncio
async def test_log_emission_on_remove_attribute(
    service: PersonService,
    session_factory,
    log_path: Path,
) -> None:
    """remove_attribute emits person.attribute.removed."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-rmlog', description='Rm')
        await session.flush()
        await service.add_attribute(session, person.id, 'key_to_remove', 'x')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        await service.remove_attribute(session, person_id, 'key_to_remove')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    removed = [r for r in records if r.get('event_type') == 'person.attribute.removed']
    assert len(removed) >= 1
    assert removed[-1]['payload']['key'] == 'key_to_remove'
