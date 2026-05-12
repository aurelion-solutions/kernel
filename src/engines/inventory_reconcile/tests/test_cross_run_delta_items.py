# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: cross-run delta item endpoints.

Covers:
- GET /inventory-reconciles/delta-items  — list with filters + cursor pagination
- GET /inventory-reconciles/delta-items/count — count with filters
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.engines.inventory_reconcile.deps import get_reconciliation_service
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
)
from src.engines.inventory_reconcile.routes import router as reconciliation_router
from src.engines.inventory_sync.deps import get_sync_apply_service
from src.engines.inventory_sync.models import SyncApplyRunStatus
from src.engines.inventory_sync.schemas import SyncApplyApplyResponse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_app(session_factory=None):
    """Minimal FastAPI app for route testing."""
    test_app = FastAPI()
    test_app.include_router(reconciliation_router)

    if session_factory is not None:

        async def override_get_db():
            async with session_factory() as session:
                yield session

        test_app.dependency_overrides[get_db] = override_get_db

    # Always wire a noop reconciliation service and sync-apply service
    mock_svc = AsyncMock()
    test_app.dependency_overrides[get_reconciliation_service] = lambda: mock_svc

    default_sync = AsyncMock()
    default_sync.apply = AsyncMock(
        return_value=SyncApplyApplyResponse(
            apply_run_id=uuid4(),
            status=SyncApplyRunStatus.completed,
            applied_count=0,
            failed_count=0,
            snapshot_ids={},
        )
    )
    test_app.dependency_overrides[get_sync_apply_service] = lambda: default_sync

    return test_app


def _make_fake_delta_item(
    run_id: UUID,
    status: ReconciliationDeltaItemStatus = ReconciliationDeltaItemStatus.pending,
    entity_type: ReconciliationEntityType = ReconciliationEntityType.access_fact,
    operation: ReconciliationDeltaOperation = ReconciliationDeltaOperation.create,
    subject_id: UUID | None = None,
    created_at: datetime | None = None,
) -> ReconciliationDeltaItem:
    item = MagicMock(spec=ReconciliationDeltaItem)
    item.id = uuid4()
    item.reconciliation_run_id = run_id
    item.entity_type = entity_type
    item.operation = operation
    item.natural_key_hash = 'a' * 64
    item.subject_id = subject_id or uuid4()
    item.account_id = None
    item.resource_id = uuid4()
    item.action_id = 1
    item.effect = 'allow'
    item.entity_id = None
    item.existing_fact_id = None
    item.source_artifact_id = uuid4()
    item.before_json = None
    item.after_json = None
    item.status = status
    item.reason = None
    item.created_at = created_at or datetime.now(UTC)
    item.applied_at = None
    return item


# ---------------------------------------------------------------------------
# GET /inventory-reconciles/delta-items tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_cross_run_returns_200_with_items(session_factory):
    """GET /delta-items → 200 with items list and application_id populated."""
    run_id = uuid4()
    app_id = uuid4()
    items = [_make_fake_delta_item(run_id) for _ in range(3)]
    # list_delta_items_cross_run returns (item, app_id) tuples
    cross_run_rows = [(item, app_id) for item in items]

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.list_delta_items_cross_run',
        new=AsyncMock(return_value=cross_run_rows),
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items')

    assert resp.status_code == 200
    body = resp.json()
    assert len(body['items']) == 3
    assert body['next_cursor'] is None
    # application_id must be present in every item
    for item in body['items']:
        assert item['application_id'] == str(app_id)


@pytest.mark.asyncio
async def test_list_cross_run_status_filter_passed_to_repo(session_factory):
    """status=pending query param is forwarded to list_delta_items_cross_run."""
    mock_list = AsyncMock(return_value=[])

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.list_delta_items_cross_run',
        new=mock_list,
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items', params={'status': 'pending'})

    assert resp.status_code == 200
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs['status'] == ReconciliationDeltaItemStatus.pending


@pytest.mark.asyncio
async def test_list_cross_run_application_id_filter(session_factory):
    """application_id query param is forwarded to list_delta_items_cross_run."""
    app_id = uuid4()
    mock_list = AsyncMock(return_value=[])

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.list_delta_items_cross_run',
        new=mock_list,
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items', params={'application_id': str(app_id)})

    assert resp.status_code == 200
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs['application_id'] == app_id


@pytest.mark.asyncio
async def test_list_cross_run_entity_type_filter(session_factory):
    """entity_type query param is forwarded to list_delta_items_cross_run."""
    mock_list = AsyncMock(return_value=[])

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.list_delta_items_cross_run',
        new=mock_list,
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items', params={'entity_type': 'person'})

    assert resp.status_code == 200
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs['entity_type'] == ReconciliationEntityType.person


@pytest.mark.asyncio
async def test_list_cross_run_pagination_next_cursor(session_factory):
    """When repo returns limit+1 rows, next_cursor is set and only limit items returned."""
    run_id = uuid4()
    app_id = uuid4()
    limit = 2
    items = [_make_fake_delta_item(run_id) for _ in range(limit + 1)]
    cross_run_rows = [(item, app_id) for item in items]

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.list_delta_items_cross_run',
        new=AsyncMock(return_value=cross_run_rows),
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items', params={'limit': limit})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body['items']) == limit
    assert body['next_cursor'] is not None


@pytest.mark.asyncio
async def test_list_cross_run_cursor_roundtrip(session_factory):
    """Cursor from page 1 can be decoded and used for page 2 without error."""
    run_id = uuid4()
    app_id = uuid4()
    limit = 1

    items_page1 = [_make_fake_delta_item(run_id), _make_fake_delta_item(run_id)]
    items_page2 = [_make_fake_delta_item(run_id)]
    rows_page1 = [(item, app_id) for item in items_page1]
    rows_page2 = [(item, app_id) for item in items_page2]

    call_count = 0

    async def mock_list(*args, **kwargs):
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            return rows_page1
        return rows_page2

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.list_delta_items_cross_run',
        new=AsyncMock(side_effect=mock_list),
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp1 = await client.get('/inventory-reconciles/delta-items', params={'limit': limit})
            assert resp1.status_code == 200
            cursor = resp1.json()['next_cursor']
            assert cursor is not None

            resp2 = await client.get('/inventory-reconciles/delta-items', params={'limit': limit, 'cursor': cursor})

    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2['items']) == 1
    assert body2['next_cursor'] is None


@pytest.mark.asyncio
async def test_list_cross_run_invalid_cursor_returns_400(session_factory):
    """An invalid cursor value → 400."""
    test_app = _make_test_app(session_factory=session_factory)

    async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
        resp = await client.get('/inventory-reconciles/delta-items', params={'cursor': 'not-a-valid-cursor'})

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /inventory-reconciles/delta-items/count tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_returns_correct_number(session_factory):
    """GET /delta-items/count → 200 with {count: N}."""
    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.count_delta_items_cross_run',
        new=AsyncMock(return_value=42),
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items/count')

    assert resp.status_code == 200
    assert resp.json() == {'count': 42}


@pytest.mark.asyncio
async def test_count_status_filter_forwarded(session_factory):
    """status=pending is forwarded to count_delta_items_cross_run."""
    mock_count = AsyncMock(return_value=5)

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.count_delta_items_cross_run',
        new=mock_count,
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items/count', params={'status': 'pending'})

    assert resp.status_code == 200
    call_kwargs = mock_count.call_args.kwargs
    assert call_kwargs['status'] == ReconciliationDeltaItemStatus.pending


@pytest.mark.asyncio
async def test_count_application_id_filter_forwarded(session_factory):
    """application_id filter is forwarded to count_delta_items_cross_run."""
    app_id = uuid4()
    mock_count = AsyncMock(return_value=7)

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.count_delta_items_cross_run',
        new=mock_count,
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items/count', params={'application_id': str(app_id)})

    assert resp.status_code == 200
    call_kwargs = mock_count.call_args.kwargs
    assert call_kwargs['application_id'] == app_id


@pytest.mark.asyncio
async def test_count_zero_when_no_items(session_factory):
    """count=0 when no items match."""
    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.count_delta_items_cross_run',
        new=AsyncMock(return_value=0),
    ):
        async with AsyncClient(transport=ASGITransport(app=test_app), base_url='http://testserver') as client:
            resp = await client.get('/inventory-reconciles/delta-items/count')

    assert resp.status_code == 200
    assert resp.json()['count'] == 0
