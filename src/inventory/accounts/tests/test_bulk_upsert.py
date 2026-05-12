# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for bulk account upsert — API (lake-first)."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.accounts.routes import router as accounts_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(accounts_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


# ---------------------------------------------------------------------------
# API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_endpoint_no_lake_returns_503(app_with_accounts) -> None:
    """POST /accounts/bulk without lake_catalog in app.state returns 503 (lake-first path)."""
    app_id = uuid.uuid4()
    payload = {
        'items': [
            {'application_id': str(app_id), 'username': 'dave', 'display_name': 'Dave D'},
        ],
    }

    async with AsyncClient(
        transport=ASGITransport(app=app_with_accounts),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/accounts/bulk', json=payload)

    # No lake configured → 503
    assert response.status_code == 503


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
