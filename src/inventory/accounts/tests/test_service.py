# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccountService."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.accounts.schemas import AccountPatch
from src.inventory.accounts.service import (
    AccountNotFoundError,
    AccountService,
    AccountSubjectNotFoundError,
)
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application
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
def service(event_service: EventService) -> AccountService:
    return AccountService(event_service=event_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_application(session) -> uuid.UUID:
    app = Application(name=f'svc-test-{uuid.uuid4()}', code=f'svc-test-{uuid.uuid4()}', config={})
    session.add(app)
    await session.flush()
    return app.id


async def _seed_subject(session) -> uuid.UUID:
    from src.inventory.customers.models import Customer

    customer = Customer(external_id=f'svc-cust-{uuid.uuid4()}')
    session.add(customer)
    await session.flush()
    subject = Subject(
        external_id=f'svc-sub-{uuid.uuid4()}',
        kind=SubjectKind.customer,
        principal_customer_id=customer.id,
        status='registered',
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def _seed_account(
    session,
    application_id: uuid.UUID,
    *,
    status: AccountStatus = AccountStatus.unknown,
    subject_id: uuid.UUID | None = None,
) -> Account:
    account = Account(
        application_id=application_id,
        username=f'user-{uuid.uuid4()}',
        status=status,
        subject_id=subject_id,
    )
    session.add(account)
    await session.flush()
    return account


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_account_status_only_emits_event(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH status=active emits inventory.account.updated with changes.status."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id, status=AccountStatus.unknown)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        patch = AccountPatch(status=AccountStatus.active)
        updated = await service.update_account(session, account_id, patch)
        await session.commit()

    assert updated.status == AccountStatus.active
    emitted = capturing_events.filter_by_type('inventory.account.updated')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.accounts'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(account_id)
    assert envelope.payload['account_id'] == str(account_id)
    assert envelope.payload['changes'] == {
        'status': {'old': AccountStatus.unknown.value, 'new': AccountStatus.active.value},
    }


@pytest.mark.asyncio
async def test_update_account_subject_only_emits_event(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH subject_id emits inventory.account.updated with changes.subject_id."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subject_id = await _seed_subject(session)
        account = await _seed_account(session, app_id)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        patch = AccountPatch(subject_id=subject_id)
        updated = await service.update_account(session, account_id, patch)
        await session.commit()

    assert updated.subject_id == subject_id
    emitted = capturing_events.filter_by_type('inventory.account.updated')
    assert len(emitted) == 1
    assert emitted[0].payload['changes'] == {
        'subject_id': {'old': None, 'new': str(subject_id)},
    }


@pytest.mark.asyncio
async def test_update_account_both_fields_emits_single_event(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH both fields emits single inventory.account.updated with both keys in changes."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        subject_id = await _seed_subject(session)
        account = await _seed_account(session, app_id, status=AccountStatus.unknown)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        patch = AccountPatch(status=AccountStatus.active, subject_id=subject_id)
        await service.update_account(session, account_id, patch)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.account.updated')
    assert len(emitted) == 1
    assert set(emitted[0].payload['changes'].keys()) == {'status', 'subject_id'}
    assert emitted[0].payload['changes']['status']['new'] == AccountStatus.active.value
    assert emitted[0].payload['changes']['subject_id']['new'] == str(subject_id)


@pytest.mark.asyncio
async def test_update_account_noop_does_not_emit(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH with same values does not emit inventory.account.updated."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id, status=AccountStatus.active)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        patch = AccountPatch(status=AccountStatus.active)
        await service.update_account(session, account_id, patch)
        await session.commit()

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_update_account_not_found_raises(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_account raises AccountNotFoundError for unknown id. No event emitted."""
    async with session_factory() as session:
        with pytest.raises(AccountNotFoundError):
            await service.update_account(session, uuid.uuid4(), AccountPatch(status=AccountStatus.active))

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_update_account_bogus_subject_raises_subject_not_found(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH with unseeded subject_id raises AccountSubjectNotFoundError. No event emitted."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id)
        account_id = account.id
        await session.commit()

    with pytest.raises(AccountSubjectNotFoundError):
        async with session_factory() as session:
            patch = AccountPatch(subject_id=uuid.uuid4())
            await service.update_account(session, account_id, patch)
            await session.commit()

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_get_account_does_not_emit_event(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_account returns account without emitting any event (Q1 — read-side audit dropped)."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id)
        account_id = account.id
        await session.commit()

    capturing_events.clear()

    async with session_factory() as session:
        found = await service.get_account(session, account_id)

    assert found is not None
    assert found.id == account_id
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_get_account_missing_returns_none_no_emit(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_account returns None and does not emit when account is not found."""
    async with session_factory() as session:
        result = await service.get_account(session, uuid.uuid4())

    assert result is None
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_update_account_propagates_correlation_id(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_account propagates an explicit correlation_id into the envelope."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id, status=AccountStatus.unknown)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        patch = AccountPatch(status=AccountStatus.active)
        await service.update_account(session, account_id, patch, correlation_id='trace-account-xyz')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.account.updated')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-account-xyz'


@pytest.mark.asyncio
async def test_update_account_generates_correlation_id_when_omitted(
    service: AccountService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_account generates a uuid4 hex correlation_id when caller omits it."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id, status=AccountStatus.unknown)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        patch = AccountPatch(status=AccountStatus.active)
        await service.update_account(session, account_id, patch)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.account.updated')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32 and all(c in '0123456789abcdef' for c in cid)
