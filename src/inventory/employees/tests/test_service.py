# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeService."""

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
def service(event_service: EventService) -> EmployeeService:
    return EmployeeService(event_service=event_service)


# ---------------------------------------------------------------------------
# Behavioural tests (state transitions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee(service: EmployeeService, session_factory) -> None:
    """create_employee creates and returns employee."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-svc', full_name='Svc')
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
        person = await create_person(session, external_id='p-get', full_name='Get')
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
        person1 = await create_person(session, external_id='p-list-1', full_name='L1')
        person2 = await create_person(session, external_id='p-list-2', full_name='L2')
        await session.flush()
        await service.create_employee(session, person_id=person1.id)
        await service.create_employee(session, person_id=person2.id)
        await session.commit()

    async with session_factory() as session:
        employees = await service.list_employees(session)
    assert len(employees) >= 2


@pytest.mark.asyncio
async def test_list_attributes(service: EmployeeService, session_factory) -> None:
    """list_attributes returns attributes for employee."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-la', full_name='LA')
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
        person = await create_person(session, external_id='p-add', full_name='Add')
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
        person = await create_person(session, external_id='p-dup', full_name='Dup')
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
        person = await create_person(session, external_id='p-rm', full_name='Rm')
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
        person = await create_person(session, external_id='p-norm', full_name='No')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.commit()
        employee_id = employee.id

    with pytest.raises(EmployeeAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, employee_id, 'nonexistent')
            await session.commit()


# ---------------------------------------------------------------------------
# Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee_emits_inventory_employee_created(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_employee emits inventory.employee.created with correct envelope fields."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-emit-c', full_name='Alice')
        await session.flush()
        employee = await service.create_employee(
            session,
            person_id=person.id,
            is_locked=False,
            description='Alice',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.employee.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.employees'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(employee.id)
    assert envelope.causation_id is None
    assert isinstance(envelope.correlation_id, str)
    assert len(envelope.correlation_id) > 0
    assert envelope.payload['employee_id'] == str(employee.id)
    assert envelope.payload['person_id'] == str(person.id)
    assert envelope.payload['is_locked'] is False
    assert envelope.payload['description'] == 'Alice'


@pytest.mark.asyncio
async def test_add_attribute_emits_inventory_employee_attribute_added(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute emits inventory.employee.attribute_added with correct envelope fields."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-emit-a', full_name='Emit')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        capturing_events.clear()
        attr = await service.add_attribute(session, employee.id, 'k1', 'v1')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.employee.attribute_added')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.event_type == 'inventory.employee.attribute_added'
    assert envelope.actor_id == 'inventory.employees'
    assert envelope.target_id == str(employee.id)
    assert envelope.payload['key'] == 'k1'
    assert envelope.payload['value'] == 'v1'
    assert envelope.payload['attribute_id'] == str(attr.id)
    assert envelope.payload['employee_id'] == str(employee.id)


@pytest.mark.asyncio
async def test_remove_attribute_emits_inventory_employee_attribute_removed(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """remove_attribute emits inventory.employee.attribute_removed with correct envelope fields."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-emit-r', full_name='Emit')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        await service.add_attribute(session, employee.id, 'key_to_remove', 'x')
        await session.commit()
        employee_id = employee.id

    async with session_factory() as session:
        capturing_events.clear()
        await service.remove_attribute(session, employee_id, 'key_to_remove')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.employee.attribute_removed')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.event_type == 'inventory.employee.attribute_removed'
    assert envelope.target_id == str(employee_id)
    assert envelope.payload['employee_id'] == str(employee_id)
    assert envelope.payload['key'] == 'key_to_remove'


# ---------------------------------------------------------------------------
# Drop-retrieved test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_employee_does_not_emit_event(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_employee emits no events (Q1 — employee.retrieved dropped)."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-noevt', full_name='NoEvt')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.commit()
        employee_id = employee.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.get_employee(session, employee_id)

    async with session_factory() as session:
        await service.get_employee(session, uuid.uuid4())

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Correlation-id plumbing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee_propagates_explicit_correlation_id(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_employee passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-corr1', full_name='Corr')
        await session.flush()
        await service.create_employee(
            session,
            person_id=person.id,
            correlation_id='corr-employee-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.employee.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-employee-xyz'


@pytest.mark.asyncio
async def test_create_employee_generates_correlation_id_when_missing(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_employee auto-generates a 32-char hex correlation_id when none is supplied."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-corr2', full_name='Corr')
        await session.flush()
        await service.create_employee(session, person_id=person.id)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.employee.created')
    assert len(emitted) == 1
    corr_id = emitted[0].correlation_id
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


@pytest.mark.asyncio
async def test_add_attribute_propagates_explicit_correlation_id(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-corr3', full_name='Corr')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        capturing_events.clear()
        await service.add_attribute(
            session,
            employee.id,
            'corr-key',
            'corr-val',
            correlation_id='corr-attr-abc',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.employee.attribute_added')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-attr-abc'


# ---------------------------------------------------------------------------
# Anti-dual-emit regression test
# ---------------------------------------------------------------------------


def test_service_has_no_log_service_attribute(service: EmployeeService) -> None:
    """EmployeeService must not have a _log attribute (DROP variant — LogService removed)."""
    assert getattr(service, '_log', None) is None
    assert not hasattr(service, '_log')
