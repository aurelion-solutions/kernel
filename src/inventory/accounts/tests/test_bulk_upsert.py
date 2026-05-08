# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for bulk account upsert — service + API."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.accounts.models import Account
from src.inventory.accounts.routes import router as accounts_router
from src.inventory.accounts.schemas import AccountBulkItem
from src.inventory.accounts.service import AccountService
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def service() -> AccountService:
    return AccountService()


@pytest.fixture
def app_with_accounts(engine):
    """FastAPI app with account routes wired to the test engine."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_application(session) -> uuid.UUID:
    app = Application(name=f'bulk-app-{uuid.uuid4()}', code=f'bulk-{uuid.uuid4()}', config={})
    session.add(app)
    await session.flush()
    return app.id


async def _get_account(session, application_id: uuid.UUID, username: str) -> Account | None:
    from sqlalchemy import select

    result = await session.execute(
        select(Account).where(
            Account.application_id == application_id,
            Account.username == username,
        )
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Service tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_bulk_inserts_new_accounts(service: AccountService, session_factory) -> None:
    """upsert_bulk inserts fresh accounts and returns their count."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        await session.commit()

    async with session_factory() as session:
        items = [
            AccountBulkItem(application_id=app_id, username='alice', display_name='Alice A', email='alice@example.com'),
            AccountBulkItem(application_id=app_id, username='bob', display_name='Bob B'),
        ]
        count = await service.upsert_bulk(session, items)
        await session.commit()

    assert count == 2

    async with session_factory() as session:
        alice = await _get_account(session, app_id, 'alice')
        bob = await _get_account(session, app_id, 'bob')

    assert alice is not None
    assert alice.display_name == 'Alice A'
    assert alice.email == 'alice@example.com'
    assert bob is not None
    assert bob.display_name == 'Bob B'
    assert bob.email is None


@pytest.mark.asyncio
async def test_upsert_bulk_updates_existing_account(service: AccountService, session_factory) -> None:
    """upsert_bulk on (application_id, username) conflict updates display_name and email."""
    async with session_factory() as session:
        app_id = await _seed_application(session)
        account = Account(application_id=app_id, username='charlie', display_name='Old Name', email='old@example.com')
        session.add(account)
        await session.commit()

    async with session_factory() as session:
        items = [
            AccountBulkItem(
                application_id=app_id,
                username='charlie',
                display_name='New Name',
                email='new@example.com',
            ),
        ]
        count = await service.upsert_bulk(session, items)
        await session.commit()

    # rowcount is 1 — the row was updated
    assert count == 1

    async with session_factory() as session:
        updated = await _get_account(session, app_id, 'charlie')

    assert updated is not None
    assert updated.display_name == 'New Name'
    assert updated.email == 'new@example.com'


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_endpoint_inserts(app_with_accounts, engine) -> None:
    """POST /accounts/bulk inserts new accounts and returns upserted count."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)
    async with sf() as session:
        app = Application(name=f'api-bulk-{uuid.uuid4()}', code=f'api-{uuid.uuid4()}', config={})
        session.add(app)
        await session.commit()
        app_id = app.id

    payload = {
        'items': [
            {'application_id': str(app_id), 'username': 'dave', 'display_name': 'Dave D'},
            {'application_id': str(app_id), 'username': 'eve'},
        ],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/accounts/bulk', json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data['upserted'] == 2


@pytest.mark.asyncio
async def test_bulk_upsert_endpoint_empty_items_returns_422(app_with_accounts) -> None:
    """POST /accounts/bulk with empty items list returns 422 (Pydantic min_length)."""
    payload = {'items': []}

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/accounts/bulk', json=payload)

    assert response.status_code == 422
