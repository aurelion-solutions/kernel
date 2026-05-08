# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Account model."""

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.inventory.accounts.models import Account, AccountStatus
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_account_instantiation_with_required_fields(session_factory):
    """Account model can be instantiated with required fields."""
    async with session_factory() as session:
        app = Application(name='test-app-inst', code='test-app-inst', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(
            application_id=app_id,
            username='alice',
            display_name='Alice',
            email='alice@example.com',
            is_active=True,
        )
        assert account.username == 'alice'
        assert account.display_name == 'Alice'
        assert account.email == 'alice@example.com'
        assert account.is_active is True


@pytest.mark.asyncio
async def test_account_id_is_uuid_primary_key(session_factory):
    """Account id is UUID primary key."""
    async with session_factory() as session:
        app = Application(name='test-app-pk', code='test-app-pk', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(application_id=app_id, username='bob')
        session.add(account)
        await session.commit()
        assert isinstance(account.id, uuid.UUID)
        assert account.id is not None


@pytest.mark.asyncio
async def test_account_is_active_and_meta_behave_correctly(session_factory):
    """is_active and meta fields behave correctly."""
    async with session_factory() as session:
        app = Application(name='test-app-meta', code='test-app-meta', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(
            application_id=app_id,
            username='charlie',
            is_active=False,
            meta={'source': 'connector', 'extra': 123},
        )
        session.add(account)
        await session.commit()
        account_id = account.id

    async with session_factory() as session:
        loaded = await session.get(Account, account_id)
        assert loaded is not None
        assert loaded.is_active is False
        assert loaded.meta == {'source': 'connector', 'extra': 123}


# ---------------------------------------------------------------------------
# Helper: create a Subject wired to a fresh Employee principal
# ---------------------------------------------------------------------------


async def _make_subject_for_account(session) -> uuid.UUID:
    """Create an employee-kind Subject wired to a fresh Employee principal."""
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='acct-test')
    await session.flush()
    employee = await create_employee(session, person_id=person.id)
    await session.flush()
    subject = Subject(
        external_id=f'acct-test-{uuid.uuid4()}',
        kind=SubjectKind.employee,
        principal_employee_id=employee.id,
        status='active',
    )
    session.add(subject)
    await session.flush()
    return subject.id


# ---------------------------------------------------------------------------
# AccountStatus tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_status_defaults_to_unknown_when_omitted(session_factory):
    """Account.status defaults to 'unknown' when not supplied."""
    async with session_factory() as session:
        app = Application(name='status-default-app', code='status-default-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(application_id=app_id, username='status-default')
        session.add(account)
        await session.commit()
        account_id = account.id

    async with session_factory() as session:
        loaded = await session.get(Account, account_id)
        assert loaded is not None
        assert loaded.status == AccountStatus.unknown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'status_value',
    [
        AccountStatus.active,
        AccountStatus.suspended,
        AccountStatus.disabled,
        AccountStatus.deleted,
        AccountStatus.unknown,
    ],
)
async def test_account_status_accepts_all_enum_values(session_factory, status_value):
    """Account.status persists all five valid enum values."""
    async with session_factory() as session:
        app = Application(
            name=f'status-enum-{status_value}',
            code=f'status-enum-{status_value}',
            config={},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(application_id=app_id, username=f'user-{status_value}', status=status_value)
        session.add(account)
        await session.commit()
        account_id = account.id

    async with session_factory() as session:
        loaded = await session.get(Account, account_id)
        assert loaded is not None
        assert loaded.status == status_value


@pytest.mark.asyncio
async def test_account_status_rejects_invalid_value_at_db_level(session_factory):
    """Account.status rejects unknown values at StrEnum construction or flush."""
    async with session_factory() as session:
        app = Application(name='status-invalid-app', code='status-invalid-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    with pytest.raises((LookupError, sa.exc.StatementError, sa.exc.DataError)):
        async with session_factory() as session:
            account = Account(application_id=app_id, username='y', status='bogus')
            session.add(account)
            await session.commit()


# ---------------------------------------------------------------------------
# subject_id FK tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_subject_id_nullable_and_defaults_to_none(session_factory):
    """Account.subject_id is NULL by default when omitted."""
    async with session_factory() as session:
        app = Application(name='subj-null-app', code='subj-null-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        account = Account(application_id=app_id, username='no-subject')
        session.add(account)
        await session.commit()
        account_id = account.id

    async with session_factory() as session:
        loaded = await session.get(Account, account_id)
        assert loaded is not None
        assert loaded.subject_id is None


@pytest.mark.asyncio
async def test_account_subject_id_fk_enforced(session_factory):
    """Account.subject_id rejects a UUID not present in subjects."""
    async with session_factory() as session:
        app = Application(name='subj-fk-app', code='subj-fk-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            account = Account(
                application_id=app_id,
                username='bad-subject',
                subject_id=uuid.uuid4(),
            )
            session.add(account)
            await session.commit()


@pytest.mark.asyncio
async def test_account_subject_id_set_null_on_subject_delete(session_factory):
    """Account.subject_id becomes NULL when the referenced Subject is deleted (ON DELETE SET NULL)."""
    async with session_factory() as session:
        app = Application(name='subj-setnull-app', code='subj-setnull-app', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        subject_id = await _make_subject_for_account(session)
        await session.commit()

    async with session_factory() as session:
        account = Account(application_id=app_id, username='bound-user', subject_id=subject_id)
        session.add(account)
        await session.commit()
        account_id = account.id

    async with session_factory() as session:
        from src.inventory.subjects.models import Subject

        subject = await session.get(Subject, subject_id)
        assert subject is not None
        await session.delete(subject)
        await session.commit()

    async with session_factory() as session:
        loaded = await session.get(Account, account_id)
        assert loaded is not None
        assert loaded.subject_id is None
