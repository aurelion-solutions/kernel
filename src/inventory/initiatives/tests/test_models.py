# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for Initiative model constraints and cascade behavior."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.initiatives.models import Initiative, InitiativeType

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


async def _make_access_fact(session) -> uuid.UUID:
    """Create an access fact via raw SQL — ORM model deleted Phase 15 Step 16."""
    import sqlalchemy as sa
    from sqlalchemy import select
    from src.inventory.actions.models import Action as RefAction

    subject_id = await _make_employee_subject(session)
    resource_id = await _make_resource(session)

    action_row = await session.execute(select(RefAction.id).where(RefAction.slug == 'read'))
    action_id = action_row.scalar_one()

    from datetime import UTC, datetime

    fact_id = uuid.uuid4()
    await session.execute(
        sa.text(
            'INSERT INTO access_facts '
            '(id, subject_id, resource_id, action_id, effect, observed_at) '
            'VALUES (:id, :subject_id, :resource_id, :action_id, :effect, :observed_at)'
        ),
        {
            'id': fact_id,
            'subject_id': subject_id,
            'resource_id': resource_id,
            'action_id': action_id,
            'effect': 'allow',
            'observed_at': datetime(2026, 1, 1, tzinfo=UTC),
        },
    )
    await session.flush()
    return fact_id


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


@pytest.mark.asyncio
async def test_initiative_fk_cascade_on_access_fact_delete(session_factory) -> None:
    """Deleting an AccessFact cascades and removes its initiatives."""
    import sqlalchemy as sa

    async with session_factory() as session:
        fact_id = await _make_access_fact(session)

        initiative = Initiative(
            access_fact_id=fact_id,
            type=InitiativeType.birthright,
            origin='Auto-assigned at birth',
        )
        session.add(initiative)
        await session.flush()
        initiative_id = initiative.id

        # Delete the access fact via raw SQL — ORM model deleted Phase 15 Step 16.
        result = await session.execute(
            sa.text('DELETE FROM access_facts WHERE id = :id RETURNING id'),
            {'id': fact_id},
        )
        assert result.scalar_one_or_none() is not None
        await session.flush()

        # Initiative should be gone via CASCADE
        from sqlalchemy import select

        result2 = await session.execute(select(Initiative).where(Initiative.id == initiative_id))
        assert result2.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_initiative_fk_rejects_unknown_access_fact(session_factory) -> None:
    """Inserting an initiative with non-existent access_fact_id raises IntegrityError (23503)."""
    async with session_factory() as session:
        initiative = Initiative(
            access_fact_id=uuid.uuid4(),  # non-existent
            type=InitiativeType.delegated,
            origin='Should fail',
        )
        session.add(initiative)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        pgcode = getattr(exc_info.value.orig, 'pgcode', None)
        assert pgcode == '23503'
