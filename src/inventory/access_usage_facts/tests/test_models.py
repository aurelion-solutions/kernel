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


async def _make_access_fact(session) -> uuid.UUID:
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.enums import Action

    subject_id = await _make_subject(session)
    resource_id = await _make_resource(session)
    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action=Action.read,
        effect=AccessFactEffect.allow,
    )
    session.add(fact)
    await session.flush()
    return fact.id


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


@pytest.mark.asyncio
async def test_cascade_delete_from_access_fact(session_factory) -> None:
    """Deleting an AccessFact cascades to its AccessUsageFact rows."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        usage_fact = AccessUsageFact(
            access_fact_id=access_fact_id,
            last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            usage_count=3,
            window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            window_to=None,
        )
        session.add(usage_fact)
        await session.flush()
        usage_fact_id = usage_fact.id
        await session.commit()

    async with session_factory() as session:
        from src.inventory.access_facts.models import AccessFact

        af = await session.get(AccessFact, access_fact_id)
        assert af is not None
        await session.delete(af)
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(AccessUsageFact).where(AccessUsageFact.id == usage_fact_id))
        assert result.scalar_one_or_none() is None
