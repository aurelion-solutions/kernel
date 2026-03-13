# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccountService."""

import json
from pathlib import Path
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
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / 'logs.jsonl'


@pytest.fixture
def log_service(log_path: Path) -> LogService:
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    return LogService(factory=factory, provider_name='file')


@pytest.fixture
def service(log_service: LogService) -> AccountService:
    return AccountService(log_service=log_service)


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


@pytest.mark.asyncio
async def test_update_account_status_only_emits_event(
    service: AccountService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH status=active emits account.updated with changed_fields=['status']."""
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
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    events = [r for r in records if r.get('event_type') == 'account.updated']
    assert len(events) == 1
    assert events[0]['payload']['changed_fields'] == ['status']


@pytest.mark.asyncio
async def test_update_account_subject_only_emits_event(
    service: AccountService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH subject_id emits account.updated with changed_fields=['subject_id']."""
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
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    events = [r for r in records if r.get('event_type') == 'account.updated']
    assert len(events) == 1
    assert events[0]['payload']['changed_fields'] == ['subject_id']


@pytest.mark.asyncio
async def test_update_account_both_fields_emits_single_event(
    service: AccountService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH both fields emits single account.updated with sorted changed_fields."""
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    events = [r for r in records if r.get('event_type') == 'account.updated']
    assert len(events) == 1
    assert events[0]['payload']['changed_fields'] == ['status', 'subject_id']


@pytest.mark.asyncio
async def test_update_account_noop_does_not_emit(
    service: AccountService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH with same values does not emit account.updated."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id, status=AccountStatus.active)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        patch = AccountPatch(status=AccountStatus.active)
        await service.update_account(session, account_id, patch)
        await session.commit()

    if log_path.exists():
        lines = log_path.read_text().strip().split('\n')
        records = [json.loads(line) for line in lines if line.strip()]
        events = [r for r in records if r.get('event_type') == 'account.updated']
        assert len(events) == 0


@pytest.mark.asyncio
async def test_update_account_not_found_raises(
    service: AccountService,
    session_factory,
) -> None:
    """update_account raises AccountNotFoundError for unknown id."""
    async with session_factory() as session:
        with pytest.raises(AccountNotFoundError):
            await service.update_account(session, uuid.uuid4(), AccountPatch(status=AccountStatus.active))


@pytest.mark.asyncio
async def test_update_account_bogus_subject_raises_subject_not_found(
    service: AccountService,
    session_factory,
) -> None:
    """PATCH with unseeded subject_id raises AccountSubjectNotFoundError."""
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


@pytest.mark.asyncio
async def test_get_account_emits_retrieved_when_found(
    service: AccountService,
    session_factory,
    log_path: Path,
) -> None:
    """get_account emits account.retrieved when account is found."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = await _seed_account(session, app_id)
        account_id = account.id
        await session.commit()

    async with session_factory() as session:
        result = await service.get_account(session, account_id)

    assert result is not None
    assert result.id == account_id
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    events = [r for r in records if r.get('event_type') == 'account.retrieved']
    assert len(events) == 1
    assert events[0]['payload']['account_id'] == str(account_id)


@pytest.mark.asyncio
async def test_get_account_missing_returns_none_no_emit(
    service: AccountService,
    session_factory,
    log_path: Path,
) -> None:
    """get_account returns None and does not emit when not found."""
    async with session_factory() as session:
        result = await service.get_account(session, uuid.uuid4())

    assert result is None
    if log_path.exists():
        lines = log_path.read_text().strip().split('\n')
        records = [json.loads(line) for line in lines if line.strip()]
        events = [r for r in records if r.get('event_type') == 'account.retrieved']
        assert len(events) == 0
