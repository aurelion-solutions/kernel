# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for NHIService."""

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
def service(event_service: EventService) -> NHIService:
    return NHIService(event_service=event_service)


# ---------------------------------------------------------------------------
# Behavioural tests (state transitions)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_nhi_emits_inventory_nhi_created(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='emit-c', name='E', kind='bot')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.nhi.created')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.nhi'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(nhi.id)
    assert envelope.causation_id is None
    assert isinstance(envelope.correlation_id, str)
    assert len(envelope.correlation_id) > 0
    assert envelope.payload == {'nhi_id': str(nhi.id), 'external_id': nhi.external_id}


@pytest.mark.asyncio
async def test_add_attribute_emits_inventory_nhi_attribute_added(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='emit-a', name='E', kind='bot')
        await session.flush()
        capturing_events.emitted.clear()
        await service.add_attribute(session, nhi.id, 'k1', 'v1')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.nhi.attribute_added')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.target_id == str(nhi.id)
    assert envelope.payload == {'nhi_id': str(nhi.id), 'key': 'k1'}


@pytest.mark.asyncio
async def test_remove_attribute_emits_inventory_nhi_attribute_removed(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='emit-r', name='E', kind='bot')
        await session.flush()
        await service.add_attribute(session, nhi.id, 'rk', 'rv')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        capturing_events.emitted.clear()
        await service.remove_attribute(session, nid, 'rk')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.nhi.attribute_removed')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.target_id == str(nid)
    assert envelope.payload == {'nhi_id': str(nid), 'key': 'rk'}


# ---------------------------------------------------------------------------
# Drop-retrieved test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_nhi_does_not_emit_event(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='no-emit', name='N', kind='bot')
        await session.commit()
        nid = nhi.id

    async with session_factory() as session:
        capturing_events.emitted.clear()
        result = await service.get_nhi(session, nid)
        assert result is not None
        assert capturing_events.emitted == []

    async with session_factory() as session:
        result = await service.get_nhi(session, uuid.uuid4())
        assert result is None
        assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Correlation-id tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_nhi_correlation_id_explicit(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    async with session_factory() as session:
        await service.create_nhi(
            session,
            external_id='corr-e',
            name='C',
            kind='bot',
            correlation_id='trace-abc-123',
        )
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.nhi.created')
    assert len(envelopes) == 1
    assert envelopes[0].correlation_id == 'trace-abc-123'


@pytest.mark.asyncio
async def test_create_nhi_correlation_id_autogenerated(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    async with session_factory() as session:
        await service.create_nhi(session, external_id='corr-a', name='C', kind='bot')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.nhi.created')
    assert len(envelopes) == 1
    corr_id = envelopes[0].correlation_id
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


@pytest.mark.asyncio
async def test_add_attribute_correlation_id_autogenerated_independent_of_create(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = await service.create_nhi(
            session,
            external_id='corr-ind',
            name='C',
            kind='bot',
            correlation_id='X',
        )
        await session.flush()
        capturing_events.emitted.clear()
        await service.add_attribute(session, nhi.id, 'ck', 'cv')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.nhi.attribute_added')
    assert len(envelopes) == 1
    corr_id = envelopes[0].correlation_id
    assert corr_id != 'X'
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


# ---------------------------------------------------------------------------
# Anti-dual-emit guard
# ---------------------------------------------------------------------------


def test_nhi_service_has_no_log_attribute() -> None:
    service = NHIService()
    assert getattr(service, '_log', None) is None
