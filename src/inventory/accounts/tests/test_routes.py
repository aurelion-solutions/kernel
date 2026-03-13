# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Account API routes."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.accounts.routes import router as accounts_router
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application


@pytest.fixture
def app_with_accounts(engine):
    """App with account routes using test engine."""
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(accounts_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _create_application(engine, name: str) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with sf() as session:
        app = Application(name=name, code=name, config={})
        session.add(app)
        await session.commit()
        return app.id


async def _create_account(
    engine,
    application_id: uuid.UUID,
    *,
    username: str = 'testuser',
    status: AccountStatus = AccountStatus.unknown,
    subject_id: uuid.UUID | None = None,
) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with sf() as session:
        account = Account(
            application_id=application_id,
            username=username,
            status=status,
            subject_id=subject_id,
        )
        session.add(account)
        await session.commit()
        return account.id


async def _create_subject(engine) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.inventory.customers.models import Customer

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with sf() as session:
        customer = Customer(external_id=f'route-cust-{uuid.uuid4()}')
        session.add(customer)
        await session.flush()
        subject = Subject(
            external_id=f'route-sub-{uuid.uuid4()}',
            kind=SubjectKind.customer,
            principal_customer_id=customer.id,
            status='registered',
        )
        session.add(subject)
        await session.commit()
        return subject.id


@pytest.mark.asyncio
async def test_get_accounts_returns_200_empty(app_with_accounts) -> None:
    """GET /accounts returns 200 and empty list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/accounts')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_accounts_filters_by_application_id(app_with_accounts, engine) -> None:
    """GET /accounts?application_id= returns only accounts in that application."""
    app1_id = await _create_application(engine, f'route-app1-{uuid.uuid4()}')
    app2_id = await _create_application(engine, f'route-app2-{uuid.uuid4()}')
    await _create_account(engine, app1_id, username='app1-user1')
    await _create_account(engine, app1_id, username='app1-user2')
    await _create_account(engine, app2_id, username='app2-user1')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/accounts?application_id={app1_id}')
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert all(a['application_id'] == str(app1_id) for a in data)


@pytest.mark.asyncio
async def test_get_accounts_filters_by_status(app_with_accounts, engine) -> None:
    """GET /accounts?status= returns only accounts with that status."""
    app_id = await _create_application(engine, f'route-status-{uuid.uuid4()}')
    await _create_account(engine, app_id, username='active-user', status=AccountStatus.active)
    await _create_account(engine, app_id, username='suspended-user', status=AccountStatus.suspended)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/accounts?status=suspended')
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]['status'] == 'suspended'


@pytest.mark.asyncio
async def test_get_accounts_filters_by_subject_id(app_with_accounts, engine) -> None:
    """GET /accounts?subject_id= returns only accounts bound to that subject."""
    app_id = await _create_application(engine, f'route-subject-{uuid.uuid4()}')
    subject_id = await _create_subject(engine)
    await _create_account(engine, app_id, username='bound-user', subject_id=subject_id)
    await _create_account(engine, app_id, username='unbound-user')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/accounts?subject_id={subject_id}')
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]['subject_id'] == str(subject_id)


@pytest.mark.asyncio
async def test_get_account_by_id_returns_200(app_with_accounts, engine) -> None:
    """GET /accounts/{id} returns 200 with full AccountRead body."""
    app_id = await _create_application(engine, f'route-get-{uuid.uuid4()}')
    subject_id = await _create_subject(engine)
    account_id = await _create_account(
        engine, app_id, username='get-user', status=AccountStatus.active, subject_id=subject_id
    )

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/accounts/{account_id}')
    assert response.status_code == 200
    data = response.json()
    assert data['id'] == str(account_id)
    assert data['status'] == 'active'
    assert data['subject_id'] == str(subject_id)
    assert 'meta' in data
    assert 'created_at' in data
    assert 'updated_at' in data


@pytest.mark.asyncio
async def test_get_account_by_id_returns_404(app_with_accounts) -> None:
    """GET /accounts/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/accounts/{uuid.uuid4()}')
    assert response.status_code == 404
    assert response.json()['detail'] == 'Account not found'


@pytest.mark.asyncio
async def test_patch_account_status_returns_200_roundtrip(app_with_accounts, engine) -> None:
    """PATCH /accounts/{id} with status returns 200 with updated status."""
    app_id = await _create_application(engine, f'route-patch-{uuid.uuid4()}')
    account_id = await _create_account(engine, app_id, username='patch-user', status=AccountStatus.active)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/accounts/{account_id}',
            json={'status': 'suspended'},
        )
    assert response.status_code == 200
    assert response.json()['status'] == 'suspended'


@pytest.mark.asyncio
async def test_patch_account_404(app_with_accounts) -> None:
    """PATCH /accounts/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/accounts/{uuid.uuid4()}',
            json={'status': 'active'},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_account_invalid_status_enum_returns_422(app_with_accounts, engine) -> None:
    """PATCH /accounts/{id} with invalid status returns 422 from Pydantic."""
    app_id = await _create_application(engine, f'route-422enum-{uuid.uuid4()}')
    account_id = await _create_account(engine, app_id, username='enum-user')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/accounts/{account_id}',
            json={'status': 'bogus'},
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_account_nonexistent_subject_returns_422(app_with_accounts, engine) -> None:
    """PATCH /accounts/{id} with unseeded subject_id returns 422."""
    app_id = await _create_application(engine, f'route-422sub-{uuid.uuid4()}')
    account_id = await _create_account(engine, app_id, username='fk-user')

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/accounts/{account_id}',
            json={'subject_id': str(uuid.uuid4())},
        )
    assert response.status_code == 422
    assert response.json()['detail'] == 'Referenced subject does not exist'


@pytest.mark.asyncio
async def test_patch_account_empty_body_returns_200_no_change(app_with_accounts, engine) -> None:
    """PATCH /accounts/{id} with empty body returns 200 without mutations."""
    app_id = await _create_application(engine, f'route-empty-{uuid.uuid4()}')
    account_id = await _create_account(engine, app_id, username='noop-user', status=AccountStatus.active)

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/accounts/{account_id}',
            json={},
        )
    assert response.status_code == 200
    assert response.json()['status'] == 'active'
