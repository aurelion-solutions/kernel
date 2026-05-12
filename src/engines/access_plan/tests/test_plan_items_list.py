# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for GET /plans/items and GET /plans/items/count endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.engines.access_plan.routes import router

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix='/api/v0')
    return app


# ---------------------------------------------------------------------------
# Fake row builder (mimics SA Row namedtuple-style)
# ---------------------------------------------------------------------------


def _fake_row(
    *,
    item_id: uuid.UUID | None = None,
    plan_id: uuid.UUID | None = None,
    plan_status: str = 'active',
    subject_ref: str | None = None,
    subject_type: str = 'employee',
    kind: str = 'grant_role',
    application: str = 'okta',
    account_ref: str | None = None,
    execution_status: str = 'proposed',
    failure_reason: object = None,
    last_verified_at: datetime | None = None,
    last_error: str | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = item_id or uuid.uuid4()
    row.plan_id = plan_id or uuid.uuid4()
    row.plan_status = plan_status
    row.subject_ref = subject_ref or str(uuid.uuid4())
    row.subject_type = subject_type
    row.kind = kind
    row.application = application
    row.account_ref = account_ref
    row.target_descriptor = {}
    row.initiatives = []
    row.initiative_refs = []
    row.policy_rule_refs = []
    row.decision_snapshot = {}
    row.execution_status = execution_status
    row.failure_reason = failure_reason
    row.last_verified_at = last_verified_at
    row.last_error = last_error
    row.created_at = created_at or datetime.now(UTC)
    return row


# ---------------------------------------------------------------------------
# Session mock helper
# ---------------------------------------------------------------------------


def _make_session_mock() -> AsyncMock:
    session = AsyncMock()
    result_mock = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock.scalars.return_value = scalars_mock
    result_mock.scalar_one_or_none.return_value = None
    result_mock.scalar_one.return_value = 0
    result_mock.fetchone.return_value = None
    result_mock.all.return_value = []
    session.execute = AsyncMock(return_value=result_mock)
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    session.flush = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Tests: GET /plans/items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_plan_items_empty() -> None:
    """GET /plans/items returns 200 with empty list when no items exist."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    with patch(
        'src.engines.access_plan.routes.list_plan_items_cross_plan',
        new=AsyncMock(return_value=([], 0)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items')

    assert resp.status_code == 200
    data = resp.json()
    assert data['items'] == []
    assert data['total'] == 0


@pytest.mark.asyncio
async def test_list_plan_items_returns_rows() -> None:
    """GET /plans/items serializes rows into PlanItemRead shapes."""
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    subject_ref = str(uuid.uuid4())
    rows = [
        _fake_row(
            item_id=item_id,
            plan_id=plan_id,
            subject_ref=subject_ref,
            execution_status='proposed',
        )
    ]
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    with patch(
        'src.engines.access_plan.routes.list_plan_items_cross_plan',
        new=AsyncMock(return_value=(rows, 1)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items')

    assert resp.status_code == 200
    data = resp.json()
    assert data['total'] == 1
    assert len(data['items']) == 1
    item = data['items'][0]
    assert item['id'] == str(item_id)
    assert item['plan_id'] == str(plan_id)
    assert item['subject_ref'] == subject_ref
    assert item['execution_status'] == 'proposed'
    assert item['plan_status'] == 'active'


@pytest.mark.asyncio
async def test_list_plan_items_execution_status_filter_single() -> None:
    """GET /plans/items?execution_status=proposed passes single filter to repository."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    captured: dict[str, Any] = {}

    async def mock_list(session, *, execution_statuses=None, **kwargs):  # type: ignore[no-untyped-def]
        captured['execution_statuses'] = execution_statuses
        return [], 0

    with patch('src.engines.access_plan.routes.list_plan_items_cross_plan', new=mock_list):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items', params={'execution_status': 'proposed'})

    assert resp.status_code == 200
    assert len(captured['execution_statuses']) == 1
    assert captured['execution_statuses'][0].value == 'proposed'


@pytest.mark.asyncio
async def test_list_plan_items_execution_status_filter_csv() -> None:
    """GET /plans/items?execution_status=proposed,executing passes two filters."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    captured: dict[str, Any] = {}

    async def mock_list(session, *, execution_statuses=None, **kwargs):  # type: ignore[no-untyped-def]
        captured['execution_statuses'] = execution_statuses
        return [], 0

    with patch('src.engines.access_plan.routes.list_plan_items_cross_plan', new=mock_list):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items', params={'execution_status': 'proposed,executing'})

    assert resp.status_code == 200
    values = {s.value for s in (captured['execution_statuses'] or [])}
    assert values == {'proposed', 'executing'}


@pytest.mark.asyncio
async def test_list_plan_items_invalid_execution_status_422() -> None:
    """GET /plans/items?execution_status=bogus returns 422."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get('/api/v0/plans/items', params={'execution_status': 'bogus'})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_plan_items_invalid_kind_422() -> None:
    """GET /plans/items?kind=not_a_kind returns 422."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get('/api/v0/plans/items', params={'kind': 'not_a_kind'})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_plan_items_plan_status_filter() -> None:
    """GET /plans/items?plan_status=active passes filter to repository."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    captured: dict[str, Any] = {}

    async def mock_list(session, *, plan_status=None, **kwargs):  # type: ignore[no-untyped-def]
        captured['plan_status'] = plan_status
        return [], 0

    with patch('src.engines.access_plan.routes.list_plan_items_cross_plan', new=mock_list):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items', params={'plan_status': 'active'})

    assert resp.status_code == 200
    assert captured['plan_status'] == 'active'


@pytest.mark.asyncio
async def test_list_plan_items_pagination() -> None:
    """GET /plans/items passes limit/offset to repository."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    captured: dict[str, Any] = {}

    async def mock_list(session, *, limit=50, offset=0, **kwargs):  # type: ignore[no-untyped-def]
        captured['limit'] = limit
        captured['offset'] = offset
        return [], 0

    with patch('src.engines.access_plan.routes.list_plan_items_cross_plan', new=mock_list):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items', params={'limit': 10, 'offset': 20})

    assert resp.status_code == 200
    assert captured['limit'] == 10
    assert captured['offset'] == 20


@pytest.mark.asyncio
async def test_list_plan_items_limit_over_200_422() -> None:
    """GET /plans/items?limit=201 returns 422."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get('/api/v0/plans/items', params={'limit': 201})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Tests: GET /plans/items/count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_plan_items_returns_count() -> None:
    """GET /plans/items/count returns {"count": N}."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    with patch(
        'src.engines.access_plan.routes.count_plan_items_cross_plan',
        new=AsyncMock(return_value=42),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items/count')

    assert resp.status_code == 200
    assert resp.json() == {'count': 42}


@pytest.mark.asyncio
async def test_count_plan_items_execution_status_filter() -> None:
    """GET /plans/items/count?execution_status=proposed passes filter."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    captured: dict[str, Any] = {}

    async def mock_count(session, *, execution_statuses=None, **kwargs):  # type: ignore[no-untyped-def]
        captured['execution_statuses'] = execution_statuses
        return 5

    with patch('src.engines.access_plan.routes.count_plan_items_cross_plan', new=mock_count):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items/count', params={'execution_status': 'proposed'})

    assert resp.status_code == 200
    assert resp.json()['count'] == 5
    assert len(captured['execution_statuses']) == 1
    assert captured['execution_statuses'][0].value == 'proposed'


@pytest.mark.asyncio
async def test_count_plan_items_invalid_status_422() -> None:
    """GET /plans/items/count?execution_status=invalid_val returns 422."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
        resp = await ac.get('/api/v0/plans/items/count', params={'execution_status': 'invalid_val'})

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_count_plan_items_plan_status_filter() -> None:
    """GET /plans/items/count?plan_status=active passes filter."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    captured: dict[str, Any] = {}

    async def mock_count(session, *, plan_status=None, **kwargs):  # type: ignore[no-untyped-def]
        captured['plan_status'] = plan_status
        return 7

    with patch('src.engines.access_plan.routes.count_plan_items_cross_plan', new=mock_count):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get(
                '/api/v0/plans/items/count',
                params={'execution_status': 'proposed', 'plan_status': 'active'},
            )

    assert resp.status_code == 200
    assert captured['plan_status'] == 'active'


# ---------------------------------------------------------------------------
# Tests: route priority — /plans/items must not match /{plan_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_items_route_not_shadowed_by_plan_id() -> None:
    """GET /plans/items must hit the items endpoint, not /{plan_id} with 'items' as UUID."""
    app = _make_app()
    app.dependency_overrides[get_db] = lambda: _make_session_mock()

    with patch(
        'src.engines.access_plan.routes.list_plan_items_cross_plan',
        new=AsyncMock(return_value=([], 0)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as ac:
            resp = await ac.get('/api/v0/plans/items')

    # Must return 200 from the items list endpoint, not 422 (UUID parse failure from /{plan_id})
    assert resp.status_code == 200
