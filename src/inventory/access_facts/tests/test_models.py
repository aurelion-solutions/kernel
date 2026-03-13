# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for AccessFact model constraints and indexes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.access_facts.models import AccessFact, AccessFactEffect
from src.inventory.enums import Action

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_employee_subject(session) -> uuid.UUID:
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


async def _make_prerequisites(session) -> tuple[uuid.UUID, uuid.UUID]:
    """Return (subject_id, resource_id)."""
    subject_id = await _make_employee_subject(session)
    resource_id = await _make_resource(session)
    return subject_id, resource_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_fact_creation_stores_all_fields(session_factory) -> None:
    """Happy path: access fact with all fields persists and created_at/valid_from auto-set."""
    async with session_factory() as session:
        subject_id, resource_id = await _make_prerequisites(session)

        fact = AccessFact(
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        session.add(fact)
        await session.flush()
        await session.refresh(fact)

        assert fact.id is not None
        assert fact.subject_id == subject_id
        assert fact.resource_id == resource_id
        assert fact.action == Action.read
        assert fact.effect == AccessFactEffect.allow
        assert fact.account_id is None
        assert fact.valid_from is not None
        assert fact.created_at is not None
        assert fact.valid_until is None


@pytest.mark.asyncio
async def test_access_fact_fk_to_subject(session_factory) -> None:
    """AccessFact with non-existent subject_id raises IntegrityError."""
    async with session_factory() as session:
        _, resource_id = await _make_prerequisites(session)

        fact = AccessFact(
            subject_id=uuid.uuid4(),
            resource_id=resource_id,
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        session.add(fact)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_access_fact_fk_to_resource(session_factory) -> None:
    """AccessFact with non-existent resource_id raises IntegrityError."""
    async with session_factory() as session:
        subject_id, _ = await _make_prerequisites(session)

        fact = AccessFact(
            subject_id=subject_id,
            resource_id=uuid.uuid4(),
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        session.add(fact)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_access_fact_uniqueness_constraint(session_factory) -> None:
    """Duplicate (subject_id, account_id, resource_id, action, effect) raises IntegrityError."""
    async with session_factory() as session:
        subject_id, resource_id = await _make_prerequisites(session)

        f1 = AccessFact(
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action=Action.write,
            effect=AccessFactEffect.allow,
        )
        session.add(f1)
        await session.flush()

        f2 = AccessFact(
            subject_id=subject_id,
            account_id=None,
            resource_id=resource_id,
            action=Action.write,
            effect=AccessFactEffect.allow,
        )
        session.add(f2)
        with pytest.raises(IntegrityError):
            await session.flush()
