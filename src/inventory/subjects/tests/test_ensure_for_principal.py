# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SubjectService.ensure_for_principal."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.service import (
    SubjectService,
    SubjectStatusRecomputePrincipalMissingError,
)
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
# Helpers
# ---------------------------------------------------------------------------


async def _make_employee(session):
    from src.inventory.employees.repository import create_employee as _repo_create_employee
    from src.inventory.persons.repository import create_person

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
    await session.flush()
    emp = await _repo_create_employee(session, person_id=person.id)
    await session.flush()
    return emp


async def _make_nhi(session):
    from src.inventory.nhi.repository import create_nhi

    nhi = await create_nhi(
        session,
        external_id=str(uuid.uuid4()),
        name='test-nhi',
        kind='service_account',
    )
    await session.flush()
    return nhi


async def _make_customer(session):
    from src.inventory.customers.repository import create_customer

    cust = await create_customer(session, external_id=str(uuid.uuid4()))
    await session.flush()
    return cust


# ---------------------------------------------------------------------------
# Tests — employee kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_creates_subject_on_first_call_employee(
    service: SubjectService,
    session_factory,
) -> None:
    """First call inserts a Subject with correct FK and status."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.subjects.models import Subject  # noqa: PLC0415

    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.ensure_for_principal(
            session,
            kind=SubjectKind.employee,
            principal_id=emp.id,
        )
        await session.commit()

    assert subject.id is not None
    assert subject.kind == SubjectKind.employee
    assert subject.principal_employee_id == emp.id
    assert subject.status == 'active'

    # Verify the row exists in DB
    async with session_factory() as session:
        row = await session.execute(sa.select(Subject).where(Subject.principal_employee_id == emp.id))
        db_subject = row.scalar_one()
        assert db_subject.id == subject.id


@pytest.mark.asyncio
async def test_ensure_is_idempotent_on_second_call_employee(
    service: SubjectService,
    session_factory,
) -> None:
    """Second call returns the same Subject without creating a duplicate."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.subjects.models import Subject  # noqa: PLC0415

    async with session_factory() as session:
        emp = await _make_employee(session)
        first = await service.ensure_for_principal(
            session,
            kind=SubjectKind.employee,
            principal_id=emp.id,
        )
        second = await service.ensure_for_principal(
            session,
            kind=SubjectKind.employee,
            principal_id=emp.id,
        )
        await session.commit()

    assert first.id == second.id

    async with session_factory() as session:
        count = (
            await session.execute(sa.select(sa.func.count()).where(Subject.principal_employee_id == emp.id))
        ).scalar()
        assert count == 1


@pytest.mark.asyncio
async def test_ensure_emits_subject_created_only_when_inserting(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Event is fired on insert; NOT fired when the row already exists."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        # First call — should emit
        await service.ensure_for_principal(session, kind=SubjectKind.employee, principal_id=emp.id)
        events_after_first = list(capturing_events.emitted)
        # Second call — should NOT emit again
        await service.ensure_for_principal(session, kind=SubjectKind.employee, principal_id=emp.id)
        await session.commit()

    created_events_after_first = [e for e in events_after_first if e.event_type == 'inventory.subject.created']
    assert len(created_events_after_first) == 1

    total_created = [e for e in capturing_events.emitted if e.event_type == 'inventory.subject.created']
    assert len(total_created) == 1  # still just one


# ---------------------------------------------------------------------------
# Tests — NHI kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_creates_subject_for_nhi(
    service: SubjectService,
    session_factory,
) -> None:
    """ensure_for_principal with kind=nhi creates a Subject with correct FK."""
    async with session_factory() as session:
        nhi = await _make_nhi(session)
        subject = await service.ensure_for_principal(
            session,
            kind=SubjectKind.nhi,
            principal_id=nhi.id,
        )
        await session.commit()

    assert subject.kind == SubjectKind.nhi
    assert subject.principal_nhi_id == nhi.id
    assert subject.status == 'active'
    assert subject.nhi_kind is not None


# ---------------------------------------------------------------------------
# Tests — customer kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_creates_subject_for_customer(
    service: SubjectService,
    session_factory,
) -> None:
    """ensure_for_principal with kind=customer derives status=registered for a fresh customer."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.ensure_for_principal(
            session,
            kind=SubjectKind.customer,
            principal_id=cust.id,
        )
        await session.commit()

    assert subject.kind == SubjectKind.customer
    assert subject.principal_customer_id == cust.id
    assert subject.status == 'registered'


# ---------------------------------------------------------------------------
# Tests — error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_raises_when_principal_missing(
    service: SubjectService,
    session_factory,
) -> None:
    """SubjectStatusRecomputePrincipalMissingError is raised when principal UUID does not exist."""
    with pytest.raises(SubjectStatusRecomputePrincipalMissingError):
        async with session_factory() as session:
            await service.ensure_for_principal(
                session,
                kind=SubjectKind.employee,
                principal_id=uuid.uuid4(),
            )
            await session.commit()
