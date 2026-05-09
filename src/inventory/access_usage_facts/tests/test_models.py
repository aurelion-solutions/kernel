# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for AccessUsageFact model constraints."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.access_usage_facts.models import AccessUsageFact

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_subject(session) -> uuid.UUID:
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
        resource_type='database',
        resource_key=str(uuid.uuid4()),
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _get_read_action_id(session) -> int:
    from sqlalchemy import select
    from src.inventory.actions.models import Action as RefAction

    result = await session.execute(select(RefAction.id).where(RefAction.slug == 'read'))
    return result.scalar_one()


async def _make_access_fact(session) -> uuid.UUID:
    """Synthesize an access_fact UUID.

    Phase 15 Step 16: PG ``access_facts`` table was dropped — facts now live in
    Iceberg. ``AccessUsageFact.access_fact_id`` is a plain UUID with no FK, so
    we just return a fresh id without seeding any prerequisites.
    """
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_usage_fact_creation_stores_all_fields(session_factory) -> None:
    """Happy path: create usage fact with window_to set; assert all columns present."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        w_to = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        last_seen = datetime(2026, 1, 1, 9, 45, 0, tzinfo=UTC)

        usage_fact = AccessUsageFact(
            access_fact_id=access_fact_id,
            last_seen=last_seen,
            usage_count=5,
            window_from=w_from,
            window_to=w_to,
        )
        session.add(usage_fact)
        await session.flush()
        await session.refresh(usage_fact)

        assert usage_fact.id is not None
        assert usage_fact.access_fact_id == access_fact_id
        assert usage_fact.usage_count == 5
        assert usage_fact.window_to is not None
        assert usage_fact.created_at is not None


@pytest.mark.asyncio
async def test_check_rejects_negative_usage_count(session_factory) -> None:
    """INSERT with usage_count=-1 must raise IntegrityError pgcode 23514."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        usage_fact = AccessUsageFact(
            access_fact_id=access_fact_id,
            last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            usage_count=-1,
            window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            window_to=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
        )
        session.add(usage_fact)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        pgcode = getattr(exc_info.value.orig, 'pgcode', None)
        assert pgcode == '23514'
