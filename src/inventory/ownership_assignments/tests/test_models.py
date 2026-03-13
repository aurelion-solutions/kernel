# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for OwnershipAssignment model constraints and indexes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.ownership_assignments.models import OwnershipAssignment, OwnershipKind

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_subject(session) -> uuid.UUID:
    """Create a minimal employee + subject, return subject.id."""
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
    """Create a minimal application + resource, return resource.id."""
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
async def test_ownership_assignment_creation_stores_all_fields(session_factory) -> None:
    """Happy path: create assignment with resource_id, verify all fields persisted."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)

        assignment = OwnershipAssignment(
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=None,
            kind=OwnershipKind.primary,
        )
        session.add(assignment)
        await session.flush()
        await session.refresh(assignment)

        assert assignment.id is not None
        assert assignment.subject_id == subject_id
        assert assignment.resource_id == resource_id
        assert assignment.account_id is None
        assert assignment.kind == OwnershipKind.primary
        assert assignment.created_at is not None


@pytest.mark.asyncio
async def test_xor_check_rejects_both_null(session_factory) -> None:
    """OwnershipAssignment with both resource_id=None and account_id=None raises IntegrityError."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)

        assignment = OwnershipAssignment(
            subject_id=subject_id,
            resource_id=None,
            account_id=None,
            kind=OwnershipKind.primary,
        )
        session.add(assignment)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert exc_info.value.orig is not None


@pytest.mark.asyncio
async def test_xor_check_rejects_both_set(session_factory) -> None:
    """OwnershipAssignment with both resource_id and account_id set raises IntegrityError."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)

        # Create a minimal account
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

        assignment = OwnershipAssignment(
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=account.id,
            kind=OwnershipKind.primary,
        )
        session.add(assignment)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        assert exc_info.value.orig is not None


@pytest.mark.asyncio
async def test_cascade_delete_from_subject(session_factory) -> None:
    """Deleting a Subject cascades to OwnershipAssignment rows."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        resource_id = await _make_resource(session)

        assignment = OwnershipAssignment(
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=None,
            kind=OwnershipKind.secondary,
        )
        session.add(assignment)
        await session.flush()
        assignment_id = assignment.id
        await session.commit()

    async with session_factory() as session:
        from src.inventory.subjects.models import Subject

        subj = await session.get(Subject, subject_id)
        assert subj is not None
        await session.delete(subj)
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(OwnershipAssignment).where(OwnershipAssignment.id == assignment_id))
        assert result.scalar_one_or_none() is None
