# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PersonService."""

import uuid

import pytest
from src.inventory.persons.repository import get_person_by_external_id
from src.inventory.persons.service import (
    DuplicatePersonAttributeError,
    PersonAttributeNotFoundError,
    PersonNotFoundError,
    PersonService,
)
from src.platform.events.schemas import EventParticipantKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(event_service: EventService) -> PersonService:
    return PersonService(event_service=event_service)


# ---------------------------------------------------------------------------
# Behavioural tests (state transitions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_person(service: PersonService, session_factory) -> None:
    """create_person creates and returns person."""
    async with session_factory() as session:
        person = await service.create_person(
            session,
            external_id='ext-svc',
            full_name='Test Person',
        )
        await session.commit()
    assert person.id is not None
    assert person.external_id == 'ext-svc'
    assert person.full_name == 'Test Person'


@pytest.mark.asyncio
async def test_get_person(service: PersonService, session_factory) -> None:
    """get_person returns person when found."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-get', full_name='Get')
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
    """list_persons returns paginated persons."""
    async with session_factory() as session:
        await service.create_person(session, external_id='ext-1', full_name='One')
        await service.create_person(session, external_id='ext-2', full_name='Two')
        await session.commit()

    async with session_factory() as session:
        persons, total = await service.list_persons(session, limit=100, offset=0)
    assert len(persons) >= 2
    assert total >= 2


@pytest.mark.asyncio
async def test_list_attributes(service: PersonService, session_factory) -> None:
    """list_attributes returns attributes for person."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-la', full_name='List')
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
        person = await service.create_person(session, external_id='ext-add', full_name='Add')
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
        person = await service.create_person(session, external_id='ext-dup', full_name='Dup')
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
        person = await service.create_person(session, external_id='ext-rm', full_name='Rm')
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
        person = await service.create_person(session, external_id='ext-norm', full_name='No')
        await session.commit()
        person_id = person.id

    with pytest.raises(PersonAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, person_id, 'nonexistent')
            await session.commit()


# ---------------------------------------------------------------------------
# Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_person_emits_inventory_person_created(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_person emits inventory.person.created with correct envelope fields."""
    async with session_factory() as session:
        person = await service.create_person(
            session,
            external_id='ext-emit-c',
            full_name='Alice',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.person.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.persons'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(person.id)
    assert envelope.causation_id is None
    assert isinstance(envelope.correlation_id, str)
    assert len(envelope.correlation_id) > 0
    assert envelope.payload['person_id'] == str(person.id)
    assert envelope.payload['external_id'] == 'ext-emit-c'


@pytest.mark.asyncio
async def test_add_attribute_emits_inventory_person_attribute_added(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute emits inventory.person.attribute_added with correct envelope fields."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-emit-a', full_name='Emit')
        await session.flush()
        capturing_events.clear()
        attr = await service.add_attribute(session, person.id, 'k1', 'v1')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.person.attribute_added')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.event_type == 'inventory.person.attribute_added'
    assert envelope.actor_id == 'inventory.persons'
    assert envelope.target_id == str(person.id)
    assert envelope.payload['key'] == 'k1'
    assert envelope.payload['value'] == 'v1'
    assert envelope.payload['attribute_id'] == str(attr.id)
    assert envelope.payload['person_id'] == str(person.id)


@pytest.mark.asyncio
async def test_remove_attribute_emits_inventory_person_attribute_removed(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """remove_attribute emits inventory.person.attribute_removed with correct envelope fields."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-emit-r', full_name='Emit')
        await session.flush()
        await service.add_attribute(session, person.id, 'key_to_remove', 'x')
        await session.commit()
        person_id = person.id

    async with session_factory() as session:
        capturing_events.clear()
        await service.remove_attribute(session, person_id, 'key_to_remove')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.person.attribute_removed')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.event_type == 'inventory.person.attribute_removed'
    assert envelope.target_id == str(person_id)
    assert envelope.payload['person_id'] == str(person_id)
    assert envelope.payload['key'] == 'key_to_remove'


# ---------------------------------------------------------------------------
# Drop-retrieved test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_person_does_not_emit_event(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_person emits no events (Q1 — person.retrieved dropped)."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='ext-noevt', full_name='NoEvt')
        await session.commit()
        person_id = person.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.get_person(session, person_id)

    async with session_factory() as session:
        await service.get_person(session, uuid.uuid4())

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Correlation-id plumbing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_person_propagates_explicit_correlation_id(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_person passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        await service.create_person(
            session,
            external_id='p-corr1',
            full_name='Corr',
            correlation_id='corr-person-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.person.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-person-xyz'


@pytest.mark.asyncio
async def test_create_person_generates_correlation_id_when_missing(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_person auto-generates a 32-char hex correlation_id when none is supplied."""
    async with session_factory() as session:
        await service.create_person(session, external_id='p-corr2', full_name='Corr')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.person.created')
    assert len(emitted) == 1
    corr_id = emitted[0].correlation_id
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


@pytest.mark.asyncio
async def test_add_attribute_propagates_explicit_correlation_id(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        person = await service.create_person(session, external_id='p-corr3', full_name='Corr')
        await session.flush()
        capturing_events.clear()
        await service.add_attribute(
            session,
            person.id,
            'corr-key',
            'corr-val',
            correlation_id='corr-attr-abc',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.person.attribute_added')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-attr-abc'


# ---------------------------------------------------------------------------
# Anti-dual-emit regression test
# ---------------------------------------------------------------------------


def test_service_has_no_log_service_attribute(service: PersonService) -> None:
    """PersonService must not have a _log attribute (DROP variant — LogService removed)."""
    assert getattr(service, '_log', None) is None
    assert not hasattr(service, '_log')
