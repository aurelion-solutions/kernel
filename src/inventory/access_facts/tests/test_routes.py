# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessFact API routes — lake-backed (Phase 15 Step 16)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.access_facts.deps import get_access_fact_service
from src.inventory.access_facts.routes import router as access_facts_router
from src.inventory.access_facts.schemas import AccessFactEffect, AccessFactView
from src.inventory.access_facts.service import AccessFactService
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _make_view(
    *,
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    action_slug: str = 'read',
    effect: AccessFactEffect = AccessFactEffect.allow,
    is_active: bool = True,
) -> AccessFactView:
    return AccessFactView(
        id=uuid.uuid4(),
        subject_id=subject_id or uuid.uuid4(),
        account_id=None,
        resource_id=resource_id or uuid.uuid4(),
        action_id=1,
        action_slug=action_slug,
        effect=effect,
        valid_from=_NOW,
        valid_until=None,
        is_active=is_active,
        revoked_at=None if is_active else datetime(2026, 1, 2, tzinfo=UTC),
        observed_at=_NOW,
        created_at=_NOW,
    )


def _make_app(service_override: AccessFactService | None = None) -> FastAPI:
    """Build a FastAPI app with access_facts router and mocked lake session."""
    noop_lake = MagicMock(spec=LakeSession)

    async def override_get_lake_session():
        yield noop_lake

    app = FastAPI()
    app.include_router(access_facts_router, prefix='/api/v0')
    app.dependency_overrides[get_lake_session] = override_get_lake_session

    if service_override is not None:
        app.dependency_overrides[get_access_fact_service] = lambda: service_override

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_access_fact_response_shape() -> None:
    """GET /access-facts/{id} returns AccessFactRead shape with action_slug."""
    fact_id = uuid.uuid4()
    view = _make_view(action_slug='administer')

    mock_svc = MagicMock(spec=AccessFactService)
    mock_svc.get_fact = AsyncMock(return_value=view)
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get(f'/api/v0/access-facts/{fact_id}')

    assert response.status_code == 200
    data = response.json()
    assert 'action_slug' in data
    assert data['action_slug'] == 'administer'
    assert 'is_active' in data
    assert data['is_active'] is True
    assert 'revoked_at' in data
    assert data['revoked_at'] is None
    assert 'action' not in data  # legacy field must be gone


@pytest.mark.asyncio
async def test_get_access_fact_not_found_returns_404() -> None:
    """GET /access-facts/{id} when not found → 404."""
    mock_svc = MagicMock(spec=AccessFactService)
    mock_svc.get_fact = AsyncMock(return_value=None)
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get(f'/api/v0/access-facts/{uuid.uuid4()}')

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_access_facts_filter_action_slug() -> None:
    """GET /access-facts?action_slug=read returns matching rows."""
    subject_id = uuid.uuid4()
    view = _make_view(subject_id=subject_id, action_slug='read')

    mock_svc = MagicMock(spec=AccessFactService)
    mock_svc.list_facts = AsyncMock(return_value=[view])
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/access-facts', params={'action_slug': 'read'})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]['action_slug'] == 'read'


@pytest.mark.asyncio
async def test_list_access_facts_filter_subject_id() -> None:
    """GET /access-facts?subject_id=... returns filtered results."""
    subject_id = uuid.uuid4()
    view = _make_view(subject_id=subject_id)

    mock_svc = MagicMock(spec=AccessFactService)
    mock_svc.list_facts = AsyncMock(return_value=[view])
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/access-facts', params={'subject_id': str(subject_id)})

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]['subject_id'] == str(subject_id)


@pytest.mark.asyncio
async def test_list_access_facts_returns_empty_for_no_matches() -> None:
    """GET /access-facts with no matching rows returns []."""
    mock_svc = MagicMock(spec=AccessFactService)
    mock_svc.list_facts = AsyncMock(return_value=[])
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/access-facts', params={'action_slug': 'nonexistent'})

    assert response.status_code == 200
    assert response.json() == []
