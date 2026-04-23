# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessFactService — Step 13 current-state store shape."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.access_facts.service import (
    AccessFactActionSlugUnknownError,
    AccessFactApplicationScopeMismatchError,
    AccessFactForeignKeyError,
    AccessFactNotFoundError,
    AccessFactNotRevokedError,
    AccessFactService,
    DuplicateActiveAccessFactError,
    _resolve_action_id,
)
from src.platform.events.schemas import EventParticipantKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService

_NOW = datetime(2026, 1, 1, tzinfo=UTC)
_SLUGS = ('read', 'write', 'execute', 'administer', 'approve', 'delegate', 'review')

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
# Seed helpers
# ---------------------------------------------------------------------------


async def _make_prerequisites(session, same_app: bool = True) -> dict:
    """Create subject + resource (+ optionally account). Return dict with ids."""
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

    ext = str(uuid.uuid4())
    resource = Resource(
        external_id=ext,
        application_id=app.id,
        kind='database',
        resource_type='database',
        resource_key=ext,
    )
    session.add(resource)
    await session.flush()

    return {
        'subject_id': subj.id,
        'resource_id': resource.id,
        'app_id': app.id,
        'account_id': None,
    }


async def _make_account_in_app(session, app_id: uuid.UUID) -> uuid.UUID:
    """Create an account bound to the given app."""
    from src.inventory.accounts.models import Account

    acc = Account(
        username=f'user-{uuid.uuid4().hex[:8]}',
        application_id=app_id,
        status='active',
    )
    session.add(acc)
    await session.flush()
    return acc.id


async def _make_account_in_different_app(session) -> uuid.UUID:
    """Create an account in a NEW separate application."""
    from src.inventory.accounts.models import Account
    from src.platform.applications.models import Application

    other_app = Application(
        name=f'other-app-{uuid.uuid4()}',
        code=f'oa-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(other_app)
    await session.flush()
    acc = Account(
        username=f'user-{uuid.uuid4().hex[:8]}',
        application_id=other_app.id,
        status='active',
    )
    session.add(acc)
    await session.flush()
    return acc.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_fact_resolves_action_slug(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact with action_slug='read' → row persisted with resolved action_id + .created event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    assert fact.id is not None
    assert fact.action_id is not None
    assert fact.is_active is True
    assert fact.revoked_at is None

    emitted = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(emitted) == 1
    env = emitted[0]
    assert env.payload['action_slug'] == 'read'
    assert env.payload['action_id'] == fact.action_id
    assert env.payload['is_active'] is True
    assert env.actor_id == 'inventory.access_facts'
    assert env.actor_kind == EventParticipantKind.CAPABILITY


@pytest.mark.asyncio
async def test_create_fact_unknown_action_slug_raises(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact with unknown action_slug → AccessFactActionSlugUnknownError, no row, no event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(AccessFactActionSlugUnknownError) as exc_info:
            await service.create_fact(
                session,
                subject_id=ids['subject_id'],
                account_id=None,
                resource_id=ids['resource_id'],
                action_slug='wat',
                effect=AccessFactEffect.allow,
                observed_at=_NOW,
            )

    assert exc_info.value.slug == 'wat'
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_fact_observed_at_required_in_signature(
    service: AccessFactService,
    session_factory,
) -> None:
    """Omitting observed_at → TypeError from keyword-only required parameter."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(TypeError):
            await service.create_fact(  # type: ignore[call-arg]
                session,
                subject_id=ids['subject_id'],
                account_id=None,
                resource_id=ids['resource_id'],
                action_slug='read',
                effect=AccessFactEffect.allow,
                # observed_at intentionally omitted
            )


@pytest.mark.asyncio
async def test_create_fact_subject_missing_raises(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact with unknown subject_id → AccessFactForeignKeyError; no event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(AccessFactForeignKeyError):
            await service.create_fact(
                session,
                subject_id=uuid.uuid4(),
                account_id=None,
                resource_id=ids['resource_id'],
                action_slug='read',
                effect=AccessFactEffect.allow,
                observed_at=_NOW,
            )
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_fact_resource_missing_raises(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact with unknown resource_id → AccessFactForeignKeyError; no event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(AccessFactForeignKeyError):
            await service.create_fact(
                session,
                subject_id=ids['subject_id'],
                account_id=None,
                resource_id=uuid.uuid4(),
                action_slug='read',
                effect=AccessFactEffect.allow,
                observed_at=_NOW,
            )
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_fact_account_missing_raises(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact with non-existent account_id → AccessFactForeignKeyError; no event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(AccessFactForeignKeyError):
            await service.create_fact(
                session,
                subject_id=ids['subject_id'],
                account_id=uuid.uuid4(),
                resource_id=ids['resource_id'],
                action_slug='read',
                effect=AccessFactEffect.allow,
                observed_at=_NOW,
            )
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_fact_application_scope_mismatch_raises(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Account in app A, resource in app B → AccessFactApplicationScopeMismatchError; no row, no event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        # Account in a different application
        account_id = await _make_account_in_different_app(session)
        with pytest.raises(AccessFactApplicationScopeMismatchError) as exc_info:
            await service.create_fact(
                session,
                subject_id=ids['subject_id'],
                account_id=account_id,
                resource_id=ids['resource_id'],
                action_slug='read',
                effect=AccessFactEffect.allow,
                observed_at=_NOW,
            )
    assert exc_info.value.account_id == account_id
    assert exc_info.value.resource_id == ids['resource_id']
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_fact_application_scope_match_passes(
    service: AccessFactService,
    session_factory,
) -> None:
    """Account and resource in the same app → row created successfully."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        account_id = await _make_account_in_app(session, ids['app_id'])
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=account_id,
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    assert fact.id is not None
    assert fact.account_id == account_id


@pytest.mark.asyncio
async def test_create_fact_subject_only_skips_scope_check(
    service: AccessFactService,
    session_factory,
) -> None:
    """account_id=None → no scope check, row created against resource in any app."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='write',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    assert fact.id is not None
    assert fact.account_id is None


@pytest.mark.asyncio
async def test_create_active_duplicate_raises_strict(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Insert twice with same key while first row is active → DuplicateActiveAccessFactError."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    capturing_events.clear()

    with pytest.raises(DuplicateActiveAccessFactError):
        async with session_factory() as session:
            await service.create_fact(
                session,
                subject_id=ids['subject_id'],
                account_id=None,
                resource_id=ids['resource_id'],
                action_slug='read',
                effect=AccessFactEffect.allow,
                observed_at=_NOW,
            )

    # No second event emitted
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_after_revoke_reactivates(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Insert → revoke → insert again with same key → reactivates same row, emits .reactivated."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()
        original_id = fact.id

    revoke_ts = datetime(2026, 1, 2, tzinfo=UTC)
    async with session_factory() as session:
        await service.revoke_fact(session, original_id, observed_at=revoke_ts)
        await session.commit()

    capturing_events.clear()

    new_ts = datetime(2026, 1, 3, tzinfo=UTC)
    async with session_factory() as session:
        reactivated = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.deny,
            observed_at=new_ts,
        )
        await session.commit()

    assert reactivated.id == original_id
    assert reactivated.is_active is True
    assert reactivated.revoked_at is None
    assert reactivated.effect == AccessFactEffect.deny
    assert reactivated.observed_at == new_ts

    emitted = capturing_events.filter_by_type('inventory.access_fact.reactivated')
    assert len(emitted) == 1
    assert emitted[0].payload['access_fact_id'] == str(original_id)
    assert emitted[0].payload['action_slug'] == 'read'
    assert emitted[0].payload['previous_revoked_at'] is not None
    # Must NOT emit .created
    assert capturing_events.filter_by_type('inventory.access_fact.created') == []


@pytest.mark.asyncio
async def test_create_after_revoke_subject_only_reactivates(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Same as reactivation test but with account_id=None (covers second partial unique)."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='write',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()
        original_id = fact.id

    async with session_factory() as session:
        await service.revoke_fact(session, original_id, observed_at=datetime(2026, 1, 2, tzinfo=UTC))
        await session.commit()

    capturing_events.clear()

    async with session_factory() as session:
        reactivated = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='write',
            effect=AccessFactEffect.allow,
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
        )
        await session.commit()

    assert reactivated.id == original_id
    assert reactivated.is_active is True
    assert capturing_events.filter_by_type('inventory.access_fact.reactivated') != []
    assert capturing_events.filter_by_type('inventory.access_fact.created') == []


@pytest.mark.asyncio
async def test_revoke_fact_idempotency_strict_error(
    service: AccessFactService,
    session_factory,
) -> None:
    """Revoking an already-revoked row → AccessFactNotRevokedError (strict, not silent no-op)."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()
        fact_id = fact.id

    async with session_factory() as session:
        await service.revoke_fact(session, fact_id, observed_at=datetime(2026, 1, 2, tzinfo=UTC))
        await session.commit()

    with pytest.raises(AccessFactNotRevokedError):
        async with session_factory() as session:
            await service.revoke_fact(session, fact_id, observed_at=datetime(2026, 1, 3, tzinfo=UTC))


@pytest.mark.asyncio
async def test_revoke_fact_emits_revoked_event(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """revoke_fact emits .revoked with exact 6-key payload; level=INFO."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action_slug='execute',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()
        fact_id = fact.id

    capturing_events.clear()
    revoke_ts = datetime(2026, 1, 2, tzinfo=UTC)

    async with session_factory() as session:
        await service.revoke_fact(session, fact_id, observed_at=revoke_ts)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_fact.revoked')
    assert len(emitted) == 1
    payload = emitted[0].payload
    # Exactly 6 keys per phase_12.md
    assert set(payload.keys()) == {'fact_id', 'subject_id', 'resource_id', 'action_id', 'action_slug', 'revoked_at'}
    assert payload['fact_id'] == str(fact_id)
    assert payload['action_slug'] == 'execute'
    assert 'account_id' not in payload  # deliberately excluded per phase_12.md


@pytest.mark.asyncio
async def test_list_facts_filter_action_slug(
    service: AccessFactService,
    session_factory,
) -> None:
    """list_facts(action_slug='read') → only facts with read action."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='write',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    async with session_factory() as session:
        results = await service.list_facts(session, action_slug='read')

    assert len(results) >= 1
    # All returned facts should have action_id matching 'read'
    action_ids = {f.action_id for f in results}
    assert len(action_ids) == 1


@pytest.mark.asyncio
async def test_list_facts_filter_unknown_action_slug_returns_empty(
    service: AccessFactService,
    session_factory,
) -> None:
    """list_facts(action_slug='wat') → [], no error (read path permissive)."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    async with session_factory() as session:
        results = await service.list_facts(session, action_slug='wat')

    assert results == []


@pytest.mark.asyncio
async def test_list_facts_filter_is_active(
    service: AccessFactService,
    session_factory,
) -> None:
    """list_facts is_active filter: True=active only, False=revoked only, None=both."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        fact_to_revoke = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='write',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    async with session_factory() as session:
        await service.revoke_fact(session, fact_to_revoke.id, observed_at=datetime(2026, 1, 2, tzinfo=UTC))
        await session.commit()

    async with session_factory() as session:
        active_only = await service.list_facts(session, subject_id=ids['subject_id'], is_active=True)
        revoked_only = await service.list_facts(session, subject_id=ids['subject_id'], is_active=False)
        both = await service.list_facts(session, subject_id=ids['subject_id'], is_active=None)

    assert all(f.is_active for f in active_only)
    assert all(not f.is_active for f in revoked_only)
    assert len(both) == len(active_only) + len(revoked_only)


@pytest.mark.asyncio
async def test_resolve_action_id_helper(session_factory) -> None:
    """_resolve_action_id covers all 7 seeded slugs + 1 unknown."""
    async with session_factory() as session:
        for slug in _SLUGS:
            action_id = await _resolve_action_id(session, slug)
            assert action_id is not None, f'Expected id for slug={slug!r}'
            assert isinstance(action_id, int)

        unknown = await _resolve_action_id(session, 'does_not_exist')
        assert unknown is None


@pytest.mark.asyncio
async def test_correlation_id_propagation(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_fact(correlation_id='abc') → event carries 'abc'; omission → fresh hex."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
            correlation_id='abc',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_fact.created')
    assert emitted[0].correlation_id == 'abc'
    capturing_events.clear()

    async with session_factory() as session:
        ids2 = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids2['subject_id'],
            resource_id=ids2['resource_id'],
            action_slug='write',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
            # correlation_id omitted
        )
        await session.commit()

    emitted2 = capturing_events.filter_by_type('inventory.access_fact.created')
    cid = emitted2[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32
    assert cid.isalnum()


# ---------------------------------------------------------------------------
# refresh_fact_fields tests (Step 14)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_fact_fields_happy_path(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """refresh_fact_fields updates effect and emits .updated event."""

    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    capturing_events.clear()

    async with session_factory() as session:
        updated = await service.refresh_fact_fields(
            session,
            fact.id,
            effect=AccessFactEffect.deny,
            valid_from=None,
            valid_until=None,
            observed_at=_NOW,
        )
        await session.commit()

    assert updated.effect == AccessFactEffect.deny
    emitted = capturing_events.filter_by_type('inventory.access_fact.updated')
    assert len(emitted) == 1
    payload = emitted[0].payload
    assert 'effect' in payload['changed_fields']
    assert payload['previous_values']['effect'] == 'allow'


@pytest.mark.asyncio
async def test_refresh_fact_fields_revoked_raises(
    service: AccessFactService,
    session_factory,
) -> None:
    """refresh_fact_fields on a revoked fact raises AccessFactNotActiveError."""
    from src.inventory.access_facts.service import AccessFactNotActiveError

    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await service.revoke_fact(session, fact.id, observed_at=_NOW)
        await session.commit()

    with pytest.raises(AccessFactNotActiveError):
        async with session_factory() as session:
            await service.refresh_fact_fields(
                session,
                fact.id,
                effect=AccessFactEffect.allow,
                valid_from=None,
                valid_until=None,
                observed_at=_NOW,
            )


@pytest.mark.asyncio
async def test_refresh_fact_fields_missing_raises(
    service: AccessFactService,
    session_factory,
) -> None:
    """refresh_fact_fields on unknown fact_id raises AccessFactNotFoundError."""
    with pytest.raises(AccessFactNotFoundError):
        async with session_factory() as session:
            await service.refresh_fact_fields(
                session,
                uuid.uuid4(),
                effect=AccessFactEffect.allow,
                valid_from=None,
                valid_until=None,
                observed_at=_NOW,
            )


@pytest.mark.asyncio
async def test_refresh_fact_fields_only_includes_changed(
    service: AccessFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Only changed fields appear in changed_fields list."""
    from datetime import timedelta

    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            resource_id=ids['resource_id'],
            action_slug='read',
            effect=AccessFactEffect.allow,
            observed_at=_NOW,
        )
        await session.commit()

    capturing_events.clear()
    new_until = _NOW + timedelta(days=90)

    async with session_factory() as session:
        await service.refresh_fact_fields(
            session,
            fact.id,
            effect=AccessFactEffect.allow,  # unchanged
            valid_from=None,  # unchanged
            valid_until=new_until,  # changed
            observed_at=_NOW,
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_fact.updated')
    assert len(emitted) == 1
    assert emitted[0].payload['changed_fields'] == ['valid_until']
