# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for InitiativeService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from src.inventory.initiatives.models import InitiativeType
from src.inventory.initiatives.schemas import InitiativePatch
from src.inventory.initiatives.service import (
    InitiativeEmptyPatchError,
    InitiativeForeignKeyError,
    InitiativeService,
)
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
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
def service(event_service: EventService) -> InitiativeService:
    return InitiativeService(event_service=event_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_access_fact(session) -> uuid.UUID:
    """Create an access fact, return fact.id."""
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.employees.repository import create_employee
    from src.inventory.enums import Action
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
    fact = AccessFact(
        subject_id=subj.id,
        resource_id=resource.id,
        action=Action.read,
        effect=AccessFactEffect.allow,
    )
    session.add(fact)
    await session.flush()
    return fact.id


# ---------------------------------------------------------------------------
# §7.1 — Rewritten from the existing suite (5 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('initiative_type', list(InitiativeType))
async def test_create_initiative_happy_path(
    initiative_type: InitiativeType,
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_initiative succeeds for all 9 types and emits inventory.initiative.created."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=initiative_type,
            origin=f'test origin for {initiative_type.value}',
        )
        await session.commit()

    assert initiative.id is not None
    assert initiative.access_fact_id == fact_id
    assert initiative.type == initiative_type

    created = capturing_events.filter_by_type('inventory.initiative.created')
    assert len(created) == 1
    env = created[0]
    assert isinstance(env, EventEnvelope)
    assert env.actor_kind == EventParticipantKind.CAPABILITY
    assert env.actor_id == 'inventory.initiatives'
    assert env.target_kind == EventParticipantKind.SYSTEM
    assert env.target_id == str(initiative.id)
    assert env.causation_id is None
    assert len(env.correlation_id) == 32
    assert all(c in '0123456789abcdef' for c in env.correlation_id)
    assert env.payload['initiative_id'] == str(initiative.id)
    assert env.payload['access_fact_id'] == str(fact_id)
    assert env.payload['type'] == initiative_type.value
    assert 'origin' in env.payload


@pytest.mark.asyncio
async def test_create_initiative_bad_access_fact_raises(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_initiative raises InitiativeForeignKeyError for unknown access_fact_id."""
    async with session_factory() as session:
        with pytest.raises(InitiativeForeignKeyError):
            await service.create_initiative(
                session,
                access_fact_id=uuid.uuid4(),
                type_=InitiativeType.birthright,
                origin='should fail',
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_update_initiative_origin_only(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH origin only emits inventory.initiative.updated; no inventory.initiative.expired emitted."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.requested,
            origin='original origin',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch(origin='updated origin')
        updated = await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert updated.origin == 'updated origin'

    updated_events = capturing_events.filter_by_type('inventory.initiative.updated')
    expired_events = capturing_events.filter_by_type('inventory.initiative.expired')

    assert len(updated_events) == 1
    assert updated_events[0].payload['changed_fields'] == ['origin']
    assert updated_events[0].payload['access_fact_id'] == str(fact_id)
    assert len(expired_events) == 0


@pytest.mark.asyncio
async def test_update_initiative_sets_valid_until_in_past_emits_expired(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH valid_until to past emits both inventory.initiative.updated and inventory.initiative.expired."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.trial,
            origin='trial period',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    past_dt = datetime.now(UTC) - timedelta(minutes=1)
    async with session_factory() as session:
        patch = InitiativePatch(valid_until=past_dt)
        updated = await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert updated.valid_until is not None

    updated_events = capturing_events.filter_by_type('inventory.initiative.updated')
    expired_events = capturing_events.filter_by_type('inventory.initiative.expired')

    assert len(updated_events) >= 1
    assert len(expired_events) == 1
    assert 'at' in expired_events[0].payload
    assert expired_events[0].payload['initiative_id'] == str(initiative_id)

    # Emission order: .updated FIRST, then .expired
    updated_idx = capturing_events.emitted.index(updated_events[0])
    expired_idx = capturing_events.emitted.index(expired_events[0])
    assert updated_idx < expired_idx


@pytest.mark.asyncio
async def test_update_initiative_empty_patch_raises(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Empty InitiativePatch raises InitiativeEmptyPatchError; no event emitted."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.inherited,
            origin='some origin',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch()
        with pytest.raises(InitiativeEmptyPatchError):
            await service.update_initiative(session, initiative_id, patch)

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# §7.2 — NEW — conditional .updated no-op guard (2 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_initiative_same_origin_emits_nothing(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH with same origin value emits no events (service-layer pre-compare contract)."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='first',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch(origin='first')
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert capturing_events.filter_by_type('inventory.initiative.updated') == []
    assert capturing_events.filter_by_type('inventory.initiative.expired') == []


@pytest.mark.asyncio
async def test_update_initiative_same_valid_until_emits_nothing_and_no_expired(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH valid_until with identical future value emits nothing (pre-compare sees no diff; is_expired=False)."""
    future_dt = datetime.now(UTC) + timedelta(hours=1)
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.requested,
            origin='origin',
            valid_until=future_dt,
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch(valid_until=future_dt)
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert capturing_events.filter_by_type('inventory.initiative.updated') == []
    assert capturing_events.filter_by_type('inventory.initiative.expired') == []


# ---------------------------------------------------------------------------
# §7.3 — NEW — retrieved-drop (1 test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_initiative_does_not_emit_event(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_initiative emits no events on found or missing path (Q1 — retrieved signal dropped, audit deferred)."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='some origin',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        # Found path
        result = await service.get_initiative(session, initiative_id)
        assert result is not None
        # Missing path
        result_missing = await service.get_initiative(session, uuid.uuid4())
        assert result_missing is None

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# §7.4 — NEW — .expired WARNING-semantics-via-operation-segment (2 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_envelope_has_no_level_or_severity_field(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """The .expired envelope has no level/severity attribute or payload key.

    Warning character is encoded in the .expired operation segment; no envelope-level
    severity field exists. Step-18 rule: envelope has no `level`/`severity` field.
    """
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.trial,
            origin='trial',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    past_dt = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as session:
        patch = InitiativePatch(valid_until=past_dt)
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    expired = capturing_events.filter_by_type('inventory.initiative.expired')
    assert len(expired) == 1
    env = expired[0]

    assert not hasattr(env, 'level') or getattr(env, 'level', None) is None
    assert not hasattr(env, 'severity') or getattr(env, 'severity', None) is None
    assert 'level' not in env.payload
    assert 'severity' not in env.payload


@pytest.mark.asyncio
async def test_expired_event_type_literal_is_canonical_three_segment(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """The .expired event_type is the exact 3-segment literal 'inventory.initiative.expired'."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.trial,
            origin='trial',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    past_dt = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as session:
        patch = InitiativePatch(valid_until=past_dt)
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    expired = capturing_events.filter_by_type('inventory.initiative.expired')
    assert len(expired) == 1
    assert expired[0].event_type == 'inventory.initiative.expired'


# ---------------------------------------------------------------------------
# §7.5 — NEW — .expired guard corner cases (4 tests, includes C3 addition)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_initiative_valid_until_future_to_further_future_emits_updated_but_not_expired(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH valid_until from future→further-future: .updated fires, .expired does not (is_expired=False)."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.requested,
            origin='origin',
            valid_until=datetime.now(UTC) + timedelta(hours=1),
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch(valid_until=datetime.now(UTC) + timedelta(hours=2))
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert len(capturing_events.filter_by_type('inventory.initiative.updated')) == 1
    assert len(capturing_events.filter_by_type('inventory.initiative.expired')) == 0


@pytest.mark.asyncio
async def test_update_initiative_valid_until_past_to_past_emits_updated_but_not_expired(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH valid_until from past→past: .updated fires if value changed, .expired does not (was_active=False)."""
    past_1h = datetime.now(UTC) - timedelta(hours=1)
    past_30m = datetime.now(UTC) - timedelta(minutes=30)
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.requested,
            origin='origin',
            valid_until=past_1h,
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch(valid_until=past_30m)
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert len(capturing_events.filter_by_type('inventory.initiative.updated')) == 1
    assert len(capturing_events.filter_by_type('inventory.initiative.expired')) == 0


@pytest.mark.asyncio
async def test_update_initiative_valid_until_cleared_to_none_emits_updated_but_not_expired(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH valid_until→None: .updated fires, .expired does not (is_expired=False because new_valid_until is None)."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.requested,
            origin='origin',
            valid_until=datetime.now(UTC) + timedelta(hours=1),
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch(valid_until=None)
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert len(capturing_events.filter_by_type('inventory.initiative.updated')) == 1
    assert len(capturing_events.filter_by_type('inventory.initiative.expired')) == 0


@pytest.mark.asyncio
async def test_expired_guard_predicate_is_independent_of_updated_guard(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Pin the guard-independence invariant: .expired anchor is 'valid_until' in patch_fields,
    not 'valid_until' in changed_fields. Step-18 divergence-4 contract.

    First call: valid_until=None→past transitions active→expired; both .updated and .expired fire.
    Second call: valid_until=same_past value on already-expired initiative;
    was_active=False → zero envelopes (changed_fields empty AND was_active=False).
    """
    past_dt = datetime.now(UTC) - timedelta(minutes=5)
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.trial,
            origin='trial',
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    # First PATCH: active (valid_until=None) → expired (past_dt); both guards fire
    async with session_factory() as session:
        patch = InitiativePatch(valid_until=past_dt)
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert len(capturing_events.filter_by_type('inventory.initiative.updated')) == 1
    assert len(capturing_events.filter_by_type('inventory.initiative.expired')) == 1

    capturing_events.emitted.clear()

    # Second PATCH: already-expired → same past value; was_active=False → no .expired,
    # changed_fields empty → no .updated
    async with session_factory() as session:
        patch2 = InitiativePatch(valid_until=past_dt)
        await service.update_initiative(session, initiative_id, patch2)
        await session.commit()

    assert capturing_events.filter_by_type('inventory.initiative.updated') == []
    assert capturing_events.filter_by_type('inventory.initiative.expired') == []


# ---------------------------------------------------------------------------
# §7.6 — NEW — correlation-id plumbing (3 tests)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_initiative_correlation_id_explicit(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Explicit correlation_id is forwarded to the envelope unchanged."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='origin',
            correlation_id='trace-abc-123',
        )
        await session.commit()

    created = capturing_events.filter_by_type('inventory.initiative.created')
    assert len(created) == 1
    assert created[0].correlation_id == 'trace-abc-123'


@pytest.mark.asyncio
async def test_create_initiative_correlation_id_autogenerated(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """No correlation_id supplied → autogenerated 32-char hex string."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='origin',
        )
        await session.commit()

    created = capturing_events.filter_by_type('inventory.initiative.created')
    assert len(created) == 1
    cid = created[0].correlation_id
    assert len(cid) == 32
    assert all(c in '0123456789abcdef' for c in cid)


@pytest.mark.asyncio
async def test_update_initiative_correlation_id_autogenerated_independent_of_create(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Auto-generated correlation_id for .updated is independent of the .created correlation_id."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='origin',
            correlation_id='A' * 32,
        )
        await session.commit()
        initiative_id = initiative.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = InitiativePatch(origin='updated')
        await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    updated = capturing_events.filter_by_type('inventory.initiative.updated')
    assert len(updated) == 1
    cid = updated[0].correlation_id
    assert cid != 'A' * 32
    assert len(cid) == 32
    assert all(c in '0123456789abcdef' for c in cid)


# ---------------------------------------------------------------------------
# §7.7 — NEW — anti-dual-emit guard (1 test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initiative_service_has_no_log_attribute(
    service: InitiativeService,
) -> None:
    """InitiativeService has no _log attribute — guards against log-era reintroduction."""
    bare_service = InitiativeService()
    assert getattr(bare_service, '_log', None) is None
