# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SubjectService."""

import uuid

import pytest
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.schemas import SubjectPatch
from src.inventory.subjects.service import (
    DuplicateSubjectAttributeError,
    InvalidSubjectStatusForKindError,
    SubjectAttributeNotFoundError,
    SubjectNotFoundError,
    SubjectService,
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
def service(event_service: EventService) -> SubjectService:
    return SubjectService(event_service=event_service)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


async def _make_employee(session):
    """Create a Person then Employee, return employee."""
    from src.inventory.employees.repository import create_employee as _repo_create_employee
    from src.inventory.persons.repository import create_person

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
    await session.flush()
    emp = await _repo_create_employee(session, person_id=person.id)
    await session.flush()
    return emp


async def _make_nhi(session):
    """Create an NHI, return it."""
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
    """Create a Customer, return it."""
    from src.inventory.customers.repository import create_customer

    cust = await create_customer(session, external_id=str(uuid.uuid4()))
    await session.flush()
    return cust


# ---------------------------------------------------------------------------
# Behavioural tests (state transitions / raise-before-emit guards)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_subject_returns_none_when_missing(
    service: SubjectService,
    session_factory,
) -> None:
    async with session_factory() as session:
        result = await service.get_subject(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_update_subject_not_found_raises(
    service: SubjectService,
    session_factory,
) -> None:
    with pytest.raises(SubjectNotFoundError):
        async with session_factory() as session:
            await service.update_subject(session, uuid.uuid4(), SubjectPatch(status='active'))


@pytest.mark.asyncio
async def test_update_subject_invalid_status_for_kind(
    service: SubjectService,
    session_factory,
) -> None:
    """update_subject rejects status incompatible with subject kind."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-badstatus-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()
        subject_id = subject.id

    with pytest.raises(InvalidSubjectStatusForKindError):
        async with session_factory() as session:
            patch = SubjectPatch(status='expired')  # NHI status, not employee
            await service.update_subject(session, subject_id, patch)


@pytest.mark.asyncio
async def test_update_subject_noop_no_event(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_subject with no changes does not emit inventory.subject.updated."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-noop-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()
        subject_id = subject.id

    capturing_events.clear()

    async with session_factory() as session:
        patch = SubjectPatch(status='active')
        await service.update_subject(session, subject_id, patch)
        await session.commit()

    updated = capturing_events.filter_by_type('inventory.subject.updated')
    assert updated == []


@pytest.mark.asyncio
async def test_add_attribute_duplicate_raises_DuplicateSubjectAttributeError(
    service: SubjectService,
    session_factory,
) -> None:
    """add_attribute raises DuplicateSubjectAttributeError on duplicate (subject_id, key)."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-dup-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await service.add_attribute(session, subject.id, 'same_key', 'v1')
        await session.commit()
        subject_id = subject.id

    with pytest.raises(DuplicateSubjectAttributeError):
        async with session_factory() as session:
            from src.inventory.subjects.repository import get_subject_by_id

            subj = await get_subject_by_id(session, subject_id)
            assert subj is not None
            await service.add_attribute(session, subj.id, 'same_key', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_add_attribute_unknown_subject_raises_SubjectNotFoundError(
    service: SubjectService,
    session_factory,
) -> None:
    """add_attribute raises SubjectNotFoundError when subject does not exist."""
    with pytest.raises(SubjectNotFoundError):
        async with session_factory() as session:
            await service.add_attribute(session, uuid.uuid4(), 'any_key', 'v')


@pytest.mark.asyncio
async def test_remove_attribute_missing_key_raises_SubjectAttributeNotFoundError(
    service: SubjectService,
    session_factory,
) -> None:
    """remove_attribute raises SubjectAttributeNotFoundError when key does not exist."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-rmm-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.commit()
        subject_id = subject.id

    with pytest.raises(SubjectAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, subject_id, 'nonexistent')


@pytest.mark.asyncio
async def test_list_attributes_does_not_emit(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """list_attributes does not emit any events."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-attr-list-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await service.add_attribute(session, subject.id, 'k1', 'v1')
        await session.commit()
        subject_id = subject.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.list_attributes(session, subject_id)

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_subject_emits_inventory_subject_created(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_subject emits inventory.subject.created with correct envelope fields."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-emit-created-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.subjects'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(subject.id)
    assert envelope.causation_id is None
    assert isinstance(envelope.correlation_id, str)
    assert len(envelope.correlation_id) > 0
    assert envelope.payload['subject_id'] == str(subject.id)
    assert envelope.payload['kind'] == 'employee'
    assert envelope.payload['status'] == 'active'
    assert envelope.payload['principal_employee_id'] == str(emp.id)
    assert envelope.payload['nhi_kind'] is None


@pytest.mark.asyncio
async def test_update_subject_emits_inventory_subject_updated(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_subject emits inventory.subject.updated when fields change."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-emit-updated-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='hired',
        )
        await session.commit()
        subject_id = subject.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.update_subject(session, subject_id, SubjectPatch(status='active'))
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.updated')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.target_id == str(subject_id)
    assert envelope.payload['subject_id'] == str(subject_id)
    assert 'status' in envelope.payload['changed_fields']


@pytest.mark.asyncio
async def test_add_attribute_emits_inventory_subject_attribute_added(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute emits inventory.subject.attribute_added with correct envelope fields."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-emit-attr-add-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        capturing_events.clear()
        await service.add_attribute(session, subject.id, 'cost_center', 'cc-42')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.attribute_added')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_id == 'inventory.subjects'
    assert envelope.target_id == str(subject.id)
    assert envelope.payload['subject_id'] == str(subject.id)
    assert envelope.payload['key'] == 'cost_center'


@pytest.mark.asyncio
async def test_remove_attribute_emits_inventory_subject_attribute_removed(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """remove_attribute emits inventory.subject.attribute_removed with correct envelope fields."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-emit-attr-rm-001',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        await service.add_attribute(session, subject.id, 'to_remove', 'x')
        await session.commit()
        subject_id = subject.id

    async with session_factory() as session:
        capturing_events.clear()
        await service.remove_attribute(session, subject_id, 'to_remove')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.attribute_removed')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.target_id == str(subject_id)
    assert envelope.payload['subject_id'] == str(subject_id)
    assert envelope.payload['key'] == 'to_remove'


# ---------------------------------------------------------------------------
# Drop-retrieved test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_subject_does_not_emit_event(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_subject emits no events (Q1 — subject.retrieved dropped)."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        subject = await service.create_subject(
            session,
            external_id='subj-noevt-001',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='hired',
        )
        await session.commit()
        subject_id = subject.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.get_subject(session, subject_id)

    async with session_factory() as session:
        await service.get_subject(session, uuid.uuid4())

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Correlation-id plumbing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_subject_propagates_explicit_correlation_id(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_subject passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        await service.create_subject(
            session,
            external_id='subj-corr1',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
            correlation_id='corr-subject-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-subject-xyz'


@pytest.mark.asyncio
async def test_create_subject_generates_correlation_id_when_missing(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_subject auto-generates a 32-char hex correlation_id when none is supplied."""
    async with session_factory() as session:
        emp = await _make_employee(session)
        await service.create_subject(
            session,
            external_id='subj-corr2',
            kind=SubjectKind.employee,
            principal_employee_id=emp.id,
            status='active',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.created')
    assert len(emitted) == 1
    corr_id = emitted[0].correlation_id
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


@pytest.mark.asyncio
async def test_add_attribute_propagates_explicit_correlation_id(
    service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        cust = await _make_customer(session)
        subject = await service.create_subject(
            session,
            external_id='subj-corr3',
            kind=SubjectKind.customer,
            principal_customer_id=cust.id,
            status='registered',
        )
        await session.flush()
        capturing_events.clear()
        await service.add_attribute(
            session,
            subject.id,
            'corr-key',
            'corr-val',
            correlation_id='corr-attr-abc',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.subject.attribute_added')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-attr-abc'


# ---------------------------------------------------------------------------
# Anti-dual-emit regression test
# ---------------------------------------------------------------------------


def test_service_has_no_log_service_attribute(service: SubjectService) -> None:
    """SubjectService must not have a _log attribute (DROP variant — LogService removed)."""
    assert getattr(service, '_log', None) is None
    assert not hasattr(service, '_log')
