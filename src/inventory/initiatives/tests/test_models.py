# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for Initiative model constraints and cascade behavior."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.initiatives.models import Initiative, InitiativeType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_employee_subject(session) -> uuid.UUID:
    """Create a minimal employee + subject, return subject.id."""
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
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


async def _make_access_fact(session) -> uuid.UUID:
    """Synthesize an access_fact UUID.

    Phase 15 Step 16: PG ``access_facts`` table was dropped — facts now live in
    Iceberg. ``Initiative.access_fact_id`` is a plain UUID with no FK, so we
    just return a fresh id.
    """
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initiative_creation_stores_all_fields(session_factory) -> None:
    """Happy path: create initiative with all fields; verify all columns persisted."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)

        initiative = Initiative(
            access_fact_id=fact_id,
            type=InitiativeType.requested,
            origin='Created by HR workflow',
            valid_until=None,
        )
        session.add(initiative)
        await session.flush()
        await session.refresh(initiative)

        assert initiative.id is not None
        assert initiative.access_fact_id == fact_id
        assert initiative.type == InitiativeType.requested
        assert initiative.origin == 'Created by HR workflow'
        assert initiative.valid_from is not None
        assert initiative.valid_until is None
        assert initiative.created_at is not None
        assert initiative.updated_at is not None


# Phase 15 Step 16: PG access_facts table dropped — Initiative.access_fact_id
# is a plain UUID with no FK, so cascade-delete and FK-rejection tests are
# no longer applicable.
