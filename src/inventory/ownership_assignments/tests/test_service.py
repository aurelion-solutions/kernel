# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for OwnershipAssignmentService."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.ownership_assignments.models import OwnershipKind
from src.inventory.ownership_assignments.service import (
    OwnershipAssignmentDuplicateError,
    OwnershipAssignmentNotFoundError,
    OwnershipAssignmentService,
    OwnershipAssignmentTargetRequiredError,
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
def service(log_service: LogService) -> OwnershipAssignmentService:
    return OwnershipAssignmentService(log_service=log_service)


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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', list(OwnershipKind))
async def test_create_assignment_resource_happy_path(
    service: OwnershipAssignmentService,
    session_factory,
    log_path: Path,
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'ownership_assignment.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'inventory.ownership_assignments'
    assert 'assignment_id' in created[-1]['payload']


@pytest.mark.asyncio
async def test_create_assignment_account_happy_path(
    service: OwnershipAssignmentService,
    session_factory,
    log_path: Path,
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'ownership_assignment.created']
    assert len(created) >= 1


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
    log_path: Path,
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'ownership_assignment.created']
    # Only the first successful create emitted an event
    assert len(created) == 1


@pytest.mark.asyncio
async def test_delete_assignment_emits_deleted_event(
    service: OwnershipAssignmentService,
    session_factory,
    log_path: Path,
) -> None:
    """delete_assignment removes row and emits deleted event; second delete raises NotFoundError."""
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    deleted = [r for r in records if r.get('event_type') == 'ownership_assignment.deleted']
    assert len(deleted) >= 1
    assert deleted[-1]['component'] == 'inventory.ownership_assignments'
    assert deleted[-1]['payload']['assignment_id'] == str(assignment_id)

    async with session_factory() as session:
        with pytest.raises(OwnershipAssignmentNotFoundError):
            await service.delete_assignment(session, assignment_id)
