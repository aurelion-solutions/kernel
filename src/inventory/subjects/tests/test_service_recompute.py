# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SubjectService.recompute_status_for_principal."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.subjects.models import SubjectKind, SubjectNHIKind
from src.inventory.subjects.service import (
    SubjectService,
)
from src.platform.events.schemas import EventEnvelope
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
def service(event_service: EventService) -> SubjectService:
    return SubjectService(event_service=event_service)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _make_customer(session, *, is_locked: bool = False, email_verified: bool = False):
    from src.inventory.customers.repository import create_customer

    cust = await create_customer(
        session,
        external_id=str(uuid.uuid4()),
        is_locked=is_locked,
        email_verified=email_verified,
    )
    await session.flush()
    return cust


async def _make_employee(session, *, is_locked: bool = False):
    from src.inventory.employees.repository import create_employee as _repo_create_employee
    from src.inventory.persons.repository import create_person

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
    await session.flush()
    emp = await _repo_create_employee(session, person_id=person.id, is_locked=is_locked)
    await session.flush()
    return emp


async def _make_nhi(session, *, is_locked: bool = False):
    from src.inventory.nhi.repository import create_nhi

    nhi = await create_nhi(
        session,
        external_id=str(uuid.uuid4()),
        name='test-nhi',
        kind='service_account',
        is_locked=is_locked,
    )
    await session.flush()
    return nhi


async def _make_subject_for_customer(service, session, customer, *, status: str = 'registered'):
    return await service.create_subject(
        session,
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.customer,
        principal_customer_id=customer.id,
        status=status,
    )


async def _make_subject_for_employee(service, session, employee, *, status: str = 'active'):
    return await service.create_subject(
        session,
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=employee.id,
        status=status,
    )


async def _make_subject_for_nhi(service, session, nhi, *, status: str = 'active'):
    return await service.create_subject(
        session,
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status=status,
    )


def _status_changed_events(capturing_events: CapturingEventService) -> list[EventEnvelope]:
    return capturing_events.filter_by_type('inventory.subject.status_changed')


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_status_unchanged_emits_no_event(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """No event when derived status equals stored status."""
    async with session_factory() as session:
        customer = await _make_customer(session, is_locked=False, email_verified=False)
        subject = await _make_subject_for_customer(service, session, customer, status='registered')
        await session.commit()
        customer_id = customer.id

    capturing_events.clear()

    async with session_factory() as session:
        result = await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=customer_id,
        )
        await session.commit()

    assert result is not None
    assert result.id == subject.id
    events = _status_changed_events(capturing_events)
    assert len(events) == 0


@pytest.mark.asyncio
async def test_recompute_status_changed_emits_event(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Flipping is_locked=True on Customer triggers inventory.subject.status_changed."""
    async with session_factory() as session:
        customer = await _make_customer(session, is_locked=False, email_verified=True)
        await _make_subject_for_customer(service, session, customer, status='verified')
        await session.commit()
        customer_id = customer.id

    capturing_events.clear()

    # Flip is_locked directly in DB to simulate CustomerService writing the field
    async with session_factory() as session:
        from src.inventory.customers.models import Customer

        cust = await session.get(Customer, customer_id)
        assert cust is not None
        cust.is_locked = True
        await session.flush()

        result = await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=customer_id,
        )
        await session.commit()

    assert result is not None
    assert result.status == 'suspended'

    events = _status_changed_events(capturing_events)
    assert len(events) == 1
    payload = events[0].payload
    assert payload['new_status'] == 'suspended'
    assert payload['previous_status'] == 'verified'
    assert 'subject_id' in payload
    assert 'at' in payload


@pytest.mark.asyncio
async def test_recompute_status_changed_writes_previous_status_to_payload(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """previous_status in event payload matches the old stored status."""
    async with session_factory() as session:
        customer = await _make_customer(session, is_locked=False, email_verified=False)
        await _make_subject_for_customer(service, session, customer, status='registered')
        await session.commit()
        customer_id = customer.id

    capturing_events.clear()

    async with session_factory() as session:
        from src.inventory.customers.models import Customer

        cust = await session.get(Customer, customer_id)
        assert cust is not None
        cust.email_verified = True
        await session.flush()

        await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=customer_id,
        )
        await session.commit()

    events = _status_changed_events(capturing_events)
    assert len(events) == 1
    assert events[0].payload['previous_status'] == 'registered'
    assert events[0].payload['new_status'] == 'verified'


@pytest.mark.asyncio
async def test_recompute_status_loads_principal_state_correctly(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Employee kind: is_locked=True maps to on_leave."""
    async with session_factory() as session:
        employee = await _make_employee(session, is_locked=True)
        await _make_subject_for_employee(service, session, employee, status='active')
        await session.commit()
        employee_id = employee.id

    capturing_events.clear()

    async with session_factory() as session:
        result = await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.employee,
            principal_id=employee_id,
        )
        await session.commit()

    assert result is not None
    assert result.status == 'on_leave'

    events = _status_changed_events(capturing_events)
    assert len(events) == 1
    assert events[0].payload['new_status'] == 'on_leave'


@pytest.mark.asyncio
async def test_recompute_subject_not_found_raises(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Returns None (no error) when principal has no bound Subject (orphan)."""
    async with session_factory() as session:
        customer = await _make_customer(session)
        await session.commit()
        customer_id = customer.id

    capturing_events.clear()

    async with session_factory() as session:
        result = await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=customer_id,
        )

    assert result is None
    assert len(_status_changed_events(capturing_events)) == 0


@pytest.mark.asyncio
async def test_recompute_is_idempotent_on_second_call(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Second call with same state emits no additional event."""
    async with session_factory() as session:
        customer = await _make_customer(session, is_locked=True)
        await _make_subject_for_customer(service, session, customer, status='verified')
        await session.commit()
        customer_id = customer.id

    capturing_events.clear()

    # First call: should flip verified -> suspended and emit one event
    async with session_factory() as session:
        await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=customer_id,
        )
        await session.commit()

    count_after_first = len(_status_changed_events(capturing_events))
    assert count_after_first == 1

    # Second call: status already suspended, no event
    async with session_factory() as session:
        await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=customer_id,
        )
        await session.commit()

    count_after_second = len(_status_changed_events(capturing_events))
    assert count_after_second == 1  # unchanged


@pytest.mark.asyncio
async def test_recompute_propagates_explicit_correlation_id(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """recompute_status_for_principal passes caller-supplied correlation_id to envelope."""
    async with session_factory() as session:
        customer = await _make_customer(session, is_locked=True)
        await _make_subject_for_customer(service, session, customer, status='verified')
        await session.commit()
        customer_id = customer.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.recompute_status_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=customer_id,
            correlation_id='corr-recompute-999',
        )
        await session.commit()

    events = _status_changed_events(capturing_events)
    assert len(events) == 1
    assert events[0].correlation_id == 'corr-recompute-999'
