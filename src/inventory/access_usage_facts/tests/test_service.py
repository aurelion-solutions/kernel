# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessUsageFactService."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.access_usage_facts.service import (
    AccessUsageFactDuplicateError,
    AccessUsageFactForeignKeyError,
    AccessUsageFactService,
    AccessUsageFactWindowOrderError,
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
def service(event_service: EventService) -> AccessUsageFactService:
    return AccessUsageFactService(event_service=event_service)


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
    import sqlalchemy as sa

    subject_id = await _make_subject(session)
    resource_id = await _make_resource(session)
    action_id = await _get_read_action_id(session)
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
async def test_create_usage_fact_happy_path_closed_window(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_usage_fact with window_to set emits inventory.access_usage_fact.created event."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        w_to = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        last_seen = datetime(2026, 1, 1, 9, 45, 0, tzinfo=UTC)

        usage_fact = await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=last_seen,
            usage_count=7,
            window_from=w_from,
            window_to=w_to,
        )
        await session.commit()

    assert usage_fact.id is not None
    assert usage_fact.usage_count == 7

    emitted = capturing_events.filter_by_type('inventory.access_usage_fact.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.access_usage_facts'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(usage_fact.id)
    assert 'usage_fact_id' in envelope.payload
    assert 'access_fact_id' in envelope.payload
    assert 'last_seen' in envelope.payload
    assert 'window_from' in envelope.payload
    assert envelope.payload['window_to'] is not None


@pytest.mark.asyncio
async def test_create_usage_fact_happy_path_open_window(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_usage_fact with window_to=None (open window) emits event with null window_to."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        last_seen = datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC)

        usage_fact = await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=last_seen,
            usage_count=2,
            window_from=w_from,
            window_to=None,
        )
        await session.commit()

    assert usage_fact.window_to is None

    emitted = capturing_events.filter_by_type('inventory.access_usage_fact.created')
    assert len(emitted) == 1
    assert emitted[0].payload['window_to'] is None


@pytest.mark.asyncio
async def test_create_usage_fact_unknown_access_fact_raises_422(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Non-existent access_fact_id raises AccessUsageFactForeignKeyError; no event emitted."""
    async with session_factory() as session:
        with pytest.raises(AccessUsageFactForeignKeyError):
            await service.create_usage_fact(
                session,
                access_fact_id=uuid.uuid4(),
                last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
                usage_count=1,
                window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
                window_to=None,
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_usage_fact_rejects_inverted_window(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """window_to <= window_from raises AccessUsageFactWindowOrderError before any DB round-trip."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        w_to = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)  # earlier than w_from

        with pytest.raises(AccessUsageFactWindowOrderError):
            await service.create_usage_fact(
                session,
                access_fact_id=access_fact_id,
                last_seen=datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC),
                usage_count=1,
                window_from=w_from,
                window_to=w_to,
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_usage_fact_duplicate_window_raises_409(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Duplicate (access_fact_id, window_from, window_to) raises AccessUsageFactDuplicateError.

    Also verifies NULLS NOT DISTINCT: two rows with window_to=None are rejected.
    """
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        w_to = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        last_seen = datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC)

        # First insert (closed window) — must succeed
        await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=last_seen,
            usage_count=1,
            window_from=w_from,
            window_to=w_to,
        )
        await session.commit()

    # Baseline: one envelope captured for the successful insert
    capturing_events.clear()

    async with session_factory() as session:
        # Second insert with same closed window — must fail
        with pytest.raises(AccessUsageFactDuplicateError):
            await service.create_usage_fact(
                session,
                access_fact_id=access_fact_id,
                last_seen=last_seen,
                usage_count=2,
                window_from=w_from,
                window_to=w_to,
            )

    # No additional envelope after duplicate failure
    assert capturing_events.filter_by_type('inventory.access_usage_fact.created') == []
    capturing_events.clear()

    # NULLS NOT DISTINCT: open-ended window — first insert must succeed
    async with session_factory() as session:
        access_fact_id2 = await _make_access_fact(session)
        w_from2 = datetime(2026, 2, 1, 9, 0, 0, tzinfo=UTC)
        await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id2,
            last_seen=datetime(2026, 2, 1, 9, 30, 0, tzinfo=UTC),
            usage_count=1,
            window_from=w_from2,
            window_to=None,
        )
        await session.commit()

    capturing_events.clear()

    async with session_factory() as session:
        # Second insert with same open window (NULL) — must also fail due to NULLS NOT DISTINCT
        with pytest.raises(AccessUsageFactDuplicateError):
            await service.create_usage_fact(
                session,
                access_fact_id=access_fact_id2,
                last_seen=datetime(2026, 2, 1, 9, 45, 0, tzinfo=UTC),
                usage_count=2,
                window_from=w_from2,
                window_to=None,
            )

    # No additional envelope after second duplicate failure
    assert capturing_events.filter_by_type('inventory.access_usage_fact.created') == []


@pytest.mark.asyncio
async def test_get_usage_fact_does_not_emit_event(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_usage_fact returns fact without emitting any event (Q1 — read-side audit dropped)."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        usage_fact = await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            usage_count=3,
            window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            window_to=None,
        )
        await session.commit()
        usage_fact_id = usage_fact.id

    # Reset captured events so only get_usage_fact's effect is observed
    capturing_events.clear()

    async with session_factory() as session:
        found = await service.get_usage_fact(session, usage_fact_id)

    assert found is not None
    assert found.id == usage_fact_id
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_usage_fact_propagates_correlation_id(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_usage_fact propagates an explicit correlation_id into the envelope."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            usage_count=1,
            window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            window_to=None,
            correlation_id='trace-usage-abc',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_usage_fact.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-usage-abc'


@pytest.mark.asyncio
async def test_create_usage_fact_generates_correlation_id_when_omitted(
    service: AccessUsageFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_usage_fact generates a uuid4 hex correlation_id when caller omits it."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            usage_count=1,
            window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            window_to=datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC),
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_usage_fact.created')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32  # uuid4().hex = 32 hex chars
    assert cid.isalnum()
