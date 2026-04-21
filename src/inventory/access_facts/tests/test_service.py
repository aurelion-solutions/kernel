# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessFactService."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.access_facts.service import (
    AccessFactForeignKeyError,
    AccessFactService,
    DuplicateAccessFactError,
)
from src.inventory.enums import Action
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
def service(event_service: EventService) -> AccessFactService:
    return AccessFactService(event_service=event_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_prerequisites(session) -> dict:
    """Create employee, subject, resource. Return dict with ids."""
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind
    from src.platform.applications.models import Application

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

    return {
        'subject_id': subj.id,
        'resource_id': resource.id,
        'account_id': None,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_fact_happy_path(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact creates fact and emits inventory.access_fact.created event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        await session.commit()

    assert fact.id is not None
    assert fact.subject_id == ids['subject_id']
    assert fact.resource_id == ids['resource_id']
    assert fact.action == Action.read
    assert fact.effect == AccessFactEffect.allow

    emitted = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.payload['access_fact_id'] == str(fact.id)
    assert envelope.payload['subject_id'] == str(ids['subject_id'])
    assert envelope.payload['resource_id'] == str(ids['resource_id'])
    assert envelope.payload['action'] == 'read'
    assert envelope.payload['effect'] == 'allow'
    assert envelope.actor_id == 'inventory.access_facts'
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(fact.id)


@pytest.mark.asyncio
async def test_create_fact_duplicate(
    service: AccessFactService,
    session_factory,
) -> None:
    """create_fact raises DuplicateAccessFactError on duplicate natural key."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.write,
            effect=AccessFactEffect.allow,
        )
        await session.commit()

    with pytest.raises(DuplicateAccessFactError):
        async with session_factory() as session:
            # Use same IDs to force uniqueness violation
            await service.create_fact(
                session,
                subject_id=ids['subject_id'],
                account_id=None,
                resource_id=ids['resource_id'],
                action=Action.write,
                effect=AccessFactEffect.allow,
            )


@pytest.mark.asyncio
async def test_create_fact_bad_subject_id(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact raises AccessFactForeignKeyError for unknown subject_id; no event emitted."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(AccessFactForeignKeyError):
            await service.create_fact(
                session,
                subject_id=uuid.uuid4(),
                account_id=None,
                resource_id=ids['resource_id'],
                action=Action.read,
                effect=AccessFactEffect.allow,
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_get_fact_does_not_emit_event(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_fact returns fact without emitting any event (Q1 — read-side audit dropped)."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.execute,
            effect=AccessFactEffect.allow,
        )
        await session.commit()
        fact_id = fact.id

    # Reset captured events so only get_fact's effect is observed
    capturing_events.clear()

    async with session_factory() as session:
        found = await service.get_fact(session, fact_id)

    assert found is not None
    assert found.id == fact_id
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_invalidate_fact(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """invalidate_fact sets valid_until and emits inventory.access_fact.invalidated."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.approve,
            effect=AccessFactEffect.deny,
        )
        await session.commit()
        fact_id = fact.id

    capturing_events.clear()

    async with session_factory() as session:
        updated = await service.invalidate_fact(session, fact_id)
        await session.commit()

    assert updated.valid_until is not None

    emitted = capturing_events.filter_by_type('inventory.access_fact.invalidated')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.payload['access_fact_id'] == str(fact_id)
    assert 'at' in envelope.payload
    assert envelope.actor_id == 'inventory.access_facts'
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(fact_id)


@pytest.mark.asyncio
async def test_get_fact_by_natural_key_returns_fact_with_null_account(
    service: AccessFactService,
    session_factory,
) -> None:
    """get_fact_by_natural_key returns the fact when account_id is None (exercises NULLS NOT DISTINCT)."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        created = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        await session.commit()
        fact_id = created.id

    async with session_factory() as session:
        found = await service.get_fact_by_natural_key(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )

    assert found is not None
    assert found.id == fact_id
    assert found.account_id is None


@pytest.mark.asyncio
async def test_get_fact_by_natural_key_returns_none_when_absent(
    service: AccessFactService,
    session_factory,
) -> None:
    """get_fact_by_natural_key returns None when no matching fact exists."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await session.commit()

    async with session_factory() as session:
        found = await service.get_fact_by_natural_key(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.administer,
            effect=AccessFactEffect.deny,
        )

    assert found is None


@pytest.mark.asyncio
async def test_create_fact_on_duplicate_does_not_rollback_outer_transaction(
    service: AccessFactService,
    session_factory,
) -> None:
    """create_fact no longer rolls back outer transaction on DuplicateAccessFactError.

    Regression guard: if session.rollback() is ever re-introduced inside create_fact,
    the Resource written before the duplicate call would disappear and this test fails.
    """
    from src.inventory.resources.models import Resource

    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        # Write first fact to establish the natural key.
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        await session.commit()

    async with session_factory() as session:
        # Reload resource — it must survive regardless of what happens next.
        resource = await session.get(Resource, ids['resource_id'])
        assert resource is not None

        # Attempt duplicate inside a savepoint so the outer transaction stays open.
        with pytest.raises(DuplicateAccessFactError):
            async with session.begin_nested():
                await service.create_fact(
                    session,
                    subject_id=ids['subject_id'],
                    account_id=None,
                    resource_id=ids['resource_id'],
                    action=Action.read,
                    effect=AccessFactEffect.allow,
                )

        # The resource must still be accessible in the same session after the error —
        # proof that the outer transaction was NOT rolled back.
        still_alive = await session.get(Resource, ids['resource_id'])
        assert still_alive is not None
        assert still_alive.id == ids['resource_id']


@pytest.mark.asyncio
async def test_create_fact_propagates_correlation_id(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact propagates an explicit correlation_id into the envelope."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
            correlation_id='trace-abc',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-abc'


@pytest.mark.asyncio
async def test_create_fact_generates_correlation_id_when_omitted(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact generates a uuid4 hex correlation_id when caller omits it."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.write,
            effect=AccessFactEffect.allow,
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32  # uuid4().hex = 32 hex chars
    assert cid.isalnum()
