# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SubjectService.bulk_upsert_employee_subjects."""

from typing import Any

import pytest
from src.inventory.employees.repository import create_employee
from src.inventory.persons.repository import create_person
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.repository import create_subject
from src.inventory.subjects.schemas import SubjectBulkItem, SubjectEmployeeStatus
from src.inventory.subjects.service import (
    SubjectPrincipalAlreadyBoundError,
    SubjectService,
    UnknownPersonExternalIdsError,
    UnresolvedEmployeesForPersonsError,
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


def _item(
    external_id: str,
    person_external_id: str,
    status: SubjectEmployeeStatus = SubjectEmployeeStatus.active,
) -> SubjectBulkItem:
    return SubjectBulkItem(
        external_id=external_id,
        person_external_id=person_external_id,
        status=status,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_inserts_new_subjects(
    service: SubjectService,
    session_factory: Any,
) -> None:
    """2 persons + 2 employees → 2 subjects inserted, kind=employee, status=active."""
    async with session_factory() as session:
        p1 = await create_person(session, external_id='bulk-subj-p1', full_name='P1')
        p2 = await create_person(session, external_id='bulk-subj-p2', full_name='P2')
        await session.flush()
        emp1 = await create_employee(session, person_id=p1.id)
        emp2 = await create_employee(session, person_id=p2.id)
        await session.commit()
        emp1_id = emp1.id
        emp2_id = emp2.id

    items = [
        _item('subj-ext-001', 'bulk-subj-p1'),
        _item('subj-ext-002', 'bulk-subj-p2'),
    ]
    async with session_factory() as session:
        subjects = await service.bulk_upsert_employee_subjects(session, items)
        await session.commit()

    assert len(subjects) == 2
    for subj in subjects:
        assert subj.id is not None
        assert subj.kind == SubjectKind.employee
        assert subj.status == 'active'

    ext_ids = [s.external_id for s in subjects]
    assert 'subj-ext-001' in ext_ids
    assert 'subj-ext-002' in ext_ids

    s1 = next(s for s in subjects if s.external_id == 'subj-ext-001')
    s2 = next(s for s in subjects if s.external_id == 'subj-ext-002')
    assert s1.principal_employee_id == emp1_id
    assert s2.principal_employee_id == emp2_id


@pytest.mark.asyncio
async def test_bulk_upsert_updates_existing_by_business_key(
    service: SubjectService,
    session_factory: Any,
) -> None:
    """Pre-created subject updated by same (kind, external_id) — same id, status updated."""
    async with session_factory() as session:
        p = await create_person(session, external_id='bulk-subj-upd-p', full_name='Upd')
        await session.flush()
        emp = await create_employee(session, person_id=p.id)
        await session.flush()
        subj = await create_subject(
            session,
            external_id='subj-upd-ext',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status=SubjectEmployeeStatus.active,
        )
        await session.commit()
        original_subject_id = subj.id

    items = [_item('subj-upd-ext', 'bulk-subj-upd-p', status=SubjectEmployeeStatus.on_leave)]
    async with session_factory() as session:
        subjects = await service.bulk_upsert_employee_subjects(session, items)
        await session.commit()

    assert len(subjects) == 1
    updated = subjects[0]
    assert updated.id == original_subject_id
    assert updated.status == 'on_leave'


@pytest.mark.asyncio
async def test_bulk_upsert_unknown_person_external_id_raises(
    service: SubjectService,
    session_factory: Any,
) -> None:
    """Non-existent person_external_id → UnknownPersonExternalIdsError, no DB write."""
    items = [_item('some-ext', 'ghost-person-xyz')]
    with pytest.raises(UnknownPersonExternalIdsError) as exc_info:
        async with session_factory() as session:
            await service.bulk_upsert_employee_subjects(session, items)

    assert 'ghost-person-xyz' in exc_info.value.missing


@pytest.mark.asyncio
async def test_bulk_upsert_person_without_employee_raises(
    service: SubjectService,
    session_factory: Any,
) -> None:
    """Person exists but has no employee row → UnresolvedEmployeesForPersonsError."""
    async with session_factory() as session:
        await create_person(session, external_id='bulk-subj-nomp', full_name='NoEmp')
        await session.commit()

    items = [_item('some-ext-2', 'bulk-subj-nomp')]
    with pytest.raises(UnresolvedEmployeesForPersonsError) as exc_info:
        async with session_factory() as session:
            await service.bulk_upsert_employee_subjects(session, items)

    assert 'bulk-subj-nomp' in exc_info.value.missing


@pytest.mark.asyncio
async def test_bulk_upsert_emits_event(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory: Any,
) -> None:
    """Exactly one inventory.subject.bulk_upserted event with correct payload."""
    async with session_factory() as session:
        p1 = await create_person(session, external_id='bulk-subj-evt-p1', full_name='E1')
        p2 = await create_person(session, external_id='bulk-subj-evt-p2', full_name='E2')
        await session.flush()
        await create_employee(session, person_id=p1.id)
        await create_employee(session, person_id=p2.id)
        await session.commit()

    items = [
        _item('subj-evt-001', 'bulk-subj-evt-p1'),
        _item('subj-evt-002', 'bulk-subj-evt-p2'),
    ]
    async with session_factory() as session:
        await service.bulk_upsert_employee_subjects(session, items)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.bulk_upserted')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.payload['count'] == 2
    assert envelope.payload['kind'] == 'employee'
    assert set(envelope.payload['external_ids']) == {'subj-evt-001', 'subj-evt-002'}


@pytest.mark.asyncio
async def test_bulk_upsert_status_hired_round_trip(
    service: SubjectService,
    session_factory: Any,
) -> None:
    """status=hired is accepted and stored correctly (positive case for employee status vocab)."""
    async with session_factory() as session:
        p = await create_person(session, external_id='bulk-subj-hired-p', full_name='H')
        await session.flush()
        await create_employee(session, person_id=p.id)
        await session.commit()

    items = [_item('subj-hired-001', 'bulk-subj-hired-p', status=SubjectEmployeeStatus.hired)]
    async with session_factory() as session:
        subjects = await service.bulk_upsert_employee_subjects(session, items)
        await session.commit()

    assert len(subjects) == 1
    assert subjects[0].status == 'hired'


@pytest.mark.asyncio
async def test_bulk_upsert_employee_already_bound_to_other_subject(
    service: SubjectService,
    session_factory: Any,
) -> None:
    """Employee already bound to different Subject → SubjectPrincipalAlreadyBoundError."""
    async with session_factory() as session:
        p = await create_person(session, external_id='bulk-subj-bound-p', full_name='B')
        await session.flush()
        emp = await create_employee(session, person_id=p.id)
        await session.flush()
        # Pre-create subject with ext_id="A" bound to emp
        await create_subject(
            session,
            external_id='subj-bound-A',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status=SubjectEmployeeStatus.active,
        )
        await session.commit()

    # Now try to upsert with ext_id="B" (different!) for the same employee
    items = [_item('subj-bound-B', 'bulk-subj-bound-p')]
    with pytest.raises(SubjectPrincipalAlreadyBoundError):
        async with session_factory() as session:
            await service.bulk_upsert_employee_subjects(session, items)
