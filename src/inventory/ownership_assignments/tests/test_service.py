# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for OwnershipAssignmentService."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.ownership_assignments.models import OwnershipKind
from src.inventory.ownership_assignments.service import (
    OwnershipAssignmentDuplicateError,
    OwnershipAssignmentNotFoundError,
    OwnershipAssignmentService,
    OwnershipAssignmentTargetRequiredError,
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
def service(event_service: EventService) -> OwnershipAssignmentService:
    return OwnershipAssignmentService(event_service=event_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_subject(session) -> uuid.UUID:
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_resource(session) -> uuid.UUID:
    from src.inventory.resources.models import Resource
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app.id,
        kind='database',
    )
    session.add(resource)
    await session.flush()
    return resource.id


# ---------------------------------------------------------------------------
# Bucket 1 — Behavioural tests (state transitions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', list(OwnershipKind))
async def test_create_assignment_resource_happy_path(
    service: OwnershipAssignmentService,
    session_factory,
    kind: OwnershipKind,
) -> None:
    """create_assignment succeeds for all 3 OwnershipKind values with resource_id."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        assignment = await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=None,
            kind=kind,
        )
        await session.commit()

    assert assignment.id is not None
    assert assignment.subject_id == subject_id
    assert assignment.resource_id == resource_id
    assert assignment.kind == kind


@pytest.mark.asyncio
async def test_create_assignment_account_happy_path(
    service: OwnershipAssignmentService,
    session_factory,
) -> None:
    """create_assignment succeeds with account_id set and resource_id=None."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)

        from src.inventory.accounts.models import Account, AccountStatus
        from src.platform.applications.models import Application

        app = Application(
            name=f'test-app-{uuid.uuid4()}',
            code=f'app-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        account = Account(
            application_id=app.id,
            username=f'user-{uuid.uuid4().hex[:8]}',
            status=AccountStatus.active,
            meta={},
        )
        session.add(account)
        await session.flush()
        account_id = account.id

        assignment = await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=None,
            account_id=account_id,
            kind=OwnershipKind.technical,
        )
        await session.commit()

    assert assignment.account_id == account_id
    assert assignment.resource_id is None


@pytest.mark.asyncio
async def test_create_assignment_rejects_both_null(
    service: OwnershipAssignmentService,
    session_factory,
) -> None:
    """create_assignment raises OwnershipAssignmentTargetRequiredError before DB hit."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        with pytest.raises(OwnershipAssignmentTargetRequiredError):
            await service.create_assignment(
                session,
                subject_id=subject_id,
                resource_id=None,
                account_id=None,
                kind=OwnershipKind.primary,
            )


@pytest.mark.asyncio
async def test_create_assignment_rejects_both_set(
    service: OwnershipAssignmentService,
    session_factory,
) -> None:
    """create_assignment raises OwnershipAssignmentTargetRequiredError when both targets set."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)

        from src.inventory.accounts.models import Account, AccountStatus
        from src.platform.applications.models import Application

        app = Application(
            name=f'test-app-{uuid.uuid4()}',
            code=f'app-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        account = Account(
            application_id=app.id,
            username=f'user-{uuid.uuid4().hex[:8]}',
            status=AccountStatus.active,
            meta={},
        )
        session.add(account)
        await session.flush()

        with pytest.raises(OwnershipAssignmentTargetRequiredError):
            await service.create_assignment(
                session,
                subject_id=subject_id,
                resource_id=resource_id,
                account_id=account.id,
                kind=OwnershipKind.primary,
            )


@pytest.mark.asyncio
async def test_create_assignment_duplicate_raises_409(
    service: OwnershipAssignmentService,
    session_factory,
) -> None:
    """Duplicate (subject, resource, kind) raises OwnershipAssignmentDuplicateError."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)

        await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            kind=OwnershipKind.primary,
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(OwnershipAssignmentDuplicateError):
            await service.create_assignment(
                session,
                subject_id=subject_id,
                resource_id=resource_id,
                kind=OwnershipKind.primary,
            )


@pytest.mark.asyncio
async def test_delete_assignment_removes_row_and_second_delete_raises_not_found(
    service: OwnershipAssignmentService,
    session_factory,
) -> None:
    """delete_assignment removes row; second delete raises OwnershipAssignmentNotFoundError."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        assignment = await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            kind=OwnershipKind.secondary,
        )
        await session.commit()
        assignment_id = assignment.id

    async with session_factory() as session:
        await service.delete_assignment(session, assignment_id)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(OwnershipAssignmentNotFoundError):
            await service.delete_assignment(session, assignment_id)


# ---------------------------------------------------------------------------
# Bucket 2 — Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_assignment_emits_inventory_ownership_assignment_created(
    service: OwnershipAssignmentService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_assignment emits inventory.ownership_assignment.created with correct envelope fields."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        assignment = await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=None,
            kind=OwnershipKind.primary,
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.ownership_assignment.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.ownership_assignments'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(assignment.id)
    assert envelope.causation_id is None
    assert isinstance(envelope.correlation_id, str)
    assert len(envelope.correlation_id) > 0
    assert envelope.payload['assignment_id'] == str(assignment.id)
    assert envelope.payload['subject_id'] == str(subject_id)
    assert envelope.payload['resource_id'] == str(resource_id)
    assert envelope.payload['account_id'] is None
    assert envelope.payload['kind'] == 'primary'


@pytest.mark.asyncio
async def test_delete_assignment_emits_inventory_ownership_assignment_deleted(
    service: OwnershipAssignmentService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """delete_assignment emits inventory.ownership_assignment.deleted with correct envelope fields."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        assignment = await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            kind=OwnershipKind.secondary,
        )
        await session.commit()
        assignment_id = assignment.id

    async with session_factory() as session:
        capturing_events.clear()
        await service.delete_assignment(session, assignment_id)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.ownership_assignment.deleted')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.event_type == 'inventory.ownership_assignment.deleted'
    assert envelope.actor_id == 'inventory.ownership_assignments'
    assert envelope.target_id == str(assignment_id)
    assert envelope.payload['assignment_id'] == str(assignment_id)
    assert envelope.payload['subject_id'] == str(subject_id)
    assert envelope.payload['resource_id'] == str(resource_id)
    assert envelope.payload['account_id'] is None
    assert envelope.payload['kind'] == 'secondary'


# ---------------------------------------------------------------------------
# Bucket 3 — Drop-retrieved test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_assignment_does_not_emit_event(
    service: OwnershipAssignmentService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_assignment emits no events (Q1 — ownership_assignment.retrieved dropped)."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        assignment = await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            kind=OwnershipKind.primary,
        )
        await session.commit()
        assignment_id = assignment.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.get_assignment(session, assignment_id)

    async with session_factory() as session:
        await service.get_assignment(session, uuid.uuid4())

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Bucket 4 — Correlation-id plumbing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_assignment_propagates_explicit_correlation_id(
    service: OwnershipAssignmentService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_assignment passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            kind=OwnershipKind.primary,
            correlation_id='corr-oa-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.ownership_assignment.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-oa-xyz'


@pytest.mark.asyncio
async def test_create_assignment_generates_correlation_id_when_missing(
    service: OwnershipAssignmentService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_assignment auto-generates a 32-char hex correlation_id when none is supplied."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            kind=OwnershipKind.primary,
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.ownership_assignment.created')
    assert len(emitted) == 1
    corr_id = emitted[0].correlation_id
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


@pytest.mark.asyncio
async def test_delete_assignment_propagates_explicit_correlation_id(
    service: OwnershipAssignmentService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """delete_assignment passes caller-supplied correlation_id through to envelope."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)
        assignment = await service.create_assignment(
            session,
            subject_id=subject_id,
            resource_id=resource_id,
            kind=OwnershipKind.primary,
        )
        await session.commit()
        assignment_id = assignment.id

    async with session_factory() as session:
        capturing_events.clear()
        await service.delete_assignment(session, assignment_id, correlation_id='corr-del-abc')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.ownership_assignment.deleted')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'corr-del-abc'


# ---------------------------------------------------------------------------
# Bucket 5 — Anti-dual-emit regression test
# ---------------------------------------------------------------------------


def test_service_has_no_log_service_attribute(service: OwnershipAssignmentService) -> None:
    """OwnershipAssignmentService must not have a _log attribute (DROP variant — LogService removed)."""
    assert getattr(service, '_log', None) is None
    assert not hasattr(service, '_log')
