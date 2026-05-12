# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests: POST /inventory-reconciles/runs + GET /inventory-reconciles/runs/{id}/delta-items."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.engines.inventory_reconcile.deps import get_reconciliation_service
from src.engines.inventory_reconcile.exceptions import ReconciliationAlreadyRunningError
from src.engines.inventory_reconcile.routes import router as reconciliation_router
from src.engines.inventory_reconcile.schemas import ReconciliationRunSummary
from src.engines.inventory_sync.deps import get_sync_apply_service
from src.engines.inventory_sync.models import SyncApplyRunStatus
from src.engines.inventory_sync.schemas import SyncApplyApplyResponse
from src.platform.applications.exceptions import ApplicationNotFoundError


def _make_summary(app_id=None, run_id=None) -> ReconciliationRunSummary:
    return ReconciliationRunSummary(
        run_id=run_id or uuid4(),
        application_id=app_id or uuid4(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        artifacts_ingested=1,
        facts_created=1,
        facts_updated=0,
        facts_revoked=0,
        artifacts_unhandled=0,
    )


def _make_test_app(session_factory=None, mock_service=None, mock_sync_apply_service=None):
    """Build a minimal FastAPI app for route testing."""
    test_app = FastAPI()
    test_app.include_router(reconciliation_router)

    if session_factory is not None:

        async def override_get_db():
            async with session_factory() as session:
                yield session

        test_app.dependency_overrides[get_db] = override_get_db

    def _make_override(svc):
        async def _override():
            return svc

        return _override

    if mock_service is not None:
        test_app.dependency_overrides[get_reconciliation_service] = _make_override(mock_service)

    if mock_sync_apply_service is not None:
        test_app.dependency_overrides[get_sync_apply_service] = _make_override(mock_sync_apply_service)
    else:
        # Default: noop sync apply service that does nothing
        default_sync_apply = AsyncMock()
        default_sync_apply.apply = AsyncMock(
            return_value=SyncApplyApplyResponse(
                apply_run_id=uuid4(),
                status=SyncApplyRunStatus.completed,
                applied_count=0,
                failed_count=0,
                snapshot_ids={},
            )
        )
        test_app.dependency_overrides[get_sync_apply_service] = _make_override(default_sync_apply)

    return test_app


# ---------------------------------------------------------------------------
# POST /inventory-reconciles/runs tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_run_mode_review_returns_200(session_factory):
    """POST with mode=review → 200 and run body."""
    app_id = uuid4()
    run_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.run = AsyncMock(return_value=_make_summary(app_id=app_id, run_id=run_id))

    from src.engines.inventory_reconcile.models import (
        ReconciliationEntityType,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    fake_run = MagicMock(spec=ReconciliationRun)
    fake_run.entity_type = ReconciliationEntityType.access_fact
    fake_run.id = run_id
    fake_run.application_id = app_id
    fake_run.status = ReconciliationRunStatus.pending_apply
    fake_run.observed_snapshot_id = None
    fake_run.current_snapshot_id = None
    fake_run.observed_batch_id = None
    fake_run.created_at = datetime.now(UTC)
    fake_run.started_at = datetime.now(UTC)
    fake_run.finished_at = datetime.now(UTC)
    fake_run.created_count = 1
    fake_run.updated_count = 0
    fake_run.revoked_count = 0
    fake_run.unchanged_count = 0
    fake_run.error = None

    test_app = _make_test_app(session_factory=session_factory, mock_service=mock_svc)

    with patch(
        'src.engines.inventory_reconcile.routes.get_run',
        new=AsyncMock(return_value=fake_run),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.post(
                '/inventory-reconciles/runs',
                json={'application_id': str(app_id), 'mode': 'review'},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body['id'] == str(run_id)
    assert body['status'] == 'pending_apply'


@pytest.mark.asyncio
async def test_run_auto_apply_runs_pipeline_then_apply(session_factory):
    """POST with mode=auto_apply → runs pipeline then apply; returns 200."""
    app_id = uuid4()
    run_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.run = AsyncMock(return_value=_make_summary(app_id=app_id, run_id=run_id))

    mock_sync_apply = AsyncMock()
    mock_sync_apply.apply = AsyncMock(
        return_value=SyncApplyApplyResponse(
            apply_run_id=uuid4(),
            status=SyncApplyRunStatus.completed,
            applied_count=2,
            failed_count=0,
            snapshot_ids={'create': 99},
        )
    )

    from src.engines.inventory_reconcile.models import (
        ReconciliationEntityType,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    fake_run = MagicMock(spec=ReconciliationRun)
    fake_run.entity_type = ReconciliationEntityType.access_fact
    fake_run.id = run_id
    fake_run.application_id = app_id
    fake_run.status = ReconciliationRunStatus.applied
    fake_run.observed_snapshot_id = None
    fake_run.current_snapshot_id = None
    fake_run.observed_batch_id = None
    fake_run.created_at = datetime.now(UTC)
    fake_run.started_at = datetime.now(UTC)
    fake_run.finished_at = datetime.now(UTC)
    fake_run.created_count = 2
    fake_run.updated_count = 0
    fake_run.revoked_count = 0
    fake_run.unchanged_count = 0
    fake_run.error = None

    test_app = _make_test_app(
        session_factory=session_factory,
        mock_service=mock_svc,
        mock_sync_apply_service=mock_sync_apply,
    )

    with patch(
        'src.engines.inventory_reconcile.routes.get_run',
        new=AsyncMock(return_value=fake_run),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.post(
                '/inventory-reconciles/runs',
                json={'application_id': str(app_id), 'mode': 'auto_apply'},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body['id'] == str(run_id)
    assert body['status'] == 'applied'
    # Verify apply was called
    mock_sync_apply.apply.assert_called_once()


@pytest.mark.asyncio
async def test_post_run_unknown_application_returns_404(session_factory):
    """POST with unknown application_id → 404."""
    app_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.run = AsyncMock(side_effect=ApplicationNotFoundError(str(app_id)))

    test_app = _make_test_app(session_factory=session_factory, mock_service=mock_svc)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/inventory-reconciles/runs',
            json={'application_id': str(app_id), 'mode': 'review'},
        )

    assert resp.status_code == 404
    assert 'Application not found' in resp.json()['detail']


@pytest.mark.asyncio
async def test_post_run_already_running_returns_409(session_factory):
    """POST while run in flight → 409."""
    app_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.run = AsyncMock(side_effect=ReconciliationAlreadyRunningError(app_id))

    test_app = _make_test_app(session_factory=session_factory, mock_service=mock_svc)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/inventory-reconciles/runs',
            json={'application_id': str(app_id), 'mode': 'review'},
        )

    assert resp.status_code == 409
    assert 'already running' in resp.json()['detail'].lower()


# ---------------------------------------------------------------------------
# GET /inventory-reconciles/runs/{id} tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_run_returns_200(session_factory):
    """GET /inventory-reconciles/runs/{id} → 200 with run body."""
    run_id = uuid4()
    app_id = uuid4()

    from src.engines.inventory_reconcile.models import (
        ReconciliationEntityType,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    fake_run = MagicMock(spec=ReconciliationRun)
    fake_run.entity_type = ReconciliationEntityType.access_fact
    fake_run.id = run_id
    fake_run.application_id = app_id
    fake_run.status = ReconciliationRunStatus.pending_apply
    fake_run.observed_snapshot_id = None
    fake_run.current_snapshot_id = None
    fake_run.observed_batch_id = None
    fake_run.created_at = datetime.now(UTC)
    fake_run.started_at = datetime.now(UTC)
    fake_run.finished_at = datetime.now(UTC)
    fake_run.created_count = 0
    fake_run.updated_count = 0
    fake_run.revoked_count = 0
    fake_run.unchanged_count = 0
    fake_run.error = None

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.get_run',
        new=AsyncMock(return_value=fake_run),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.get(f'/inventory-reconciles/runs/{run_id}')

    assert resp.status_code == 200
    assert resp.json()['id'] == str(run_id)


@pytest.mark.asyncio
async def test_get_run_unknown_id_returns_404(session_factory):
    """GET /inventory-reconciles/runs/{id} with unknown id → 404."""
    run_id = uuid4()

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.get_run',
        new=AsyncMock(return_value=None),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.get(f'/inventory-reconciles/runs/{run_id}')

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /inventory-reconciles/runs/{id}/delta-items tests
# ---------------------------------------------------------------------------


def _make_fake_delta_item(run_id, created_at=None):
    """Build a fake ReconciliationDeltaItem with required fields."""
    from src.engines.inventory_reconcile.models import (
        ReconciliationDeltaItem,
        ReconciliationDeltaItemStatus,
        ReconciliationDeltaOperation,
        ReconciliationEntityType,
    )

    item = MagicMock(spec=ReconciliationDeltaItem)
    item.id = uuid4()
    item.reconciliation_run_id = run_id
    item.entity_type = ReconciliationEntityType.access_fact
    item.operation = ReconciliationDeltaOperation.create
    item.natural_key_hash = 'a' * 64
    item.subject_id = uuid4()
    item.account_id = None
    item.resource_id = uuid4()
    item.action_id = 1
    item.effect = 'allow'
    item.entity_id = None
    item.existing_fact_id = None
    item.source_artifact_id = uuid4()
    item.before_json = None
    item.after_json = None
    item.status = ReconciliationDeltaItemStatus.pending
    item.reason = None
    item.created_at = created_at or datetime.now(UTC)
    item.applied_at = None
    return item


@pytest.mark.asyncio
async def test_get_delta_items_returns_200(session_factory):
    """GET /runs/{id}/delta-items → 200 with paginated items."""
    run_id = uuid4()
    app_id = uuid4()

    from src.engines.inventory_reconcile.models import (
        ReconciliationEntityType,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    fake_run = MagicMock(spec=ReconciliationRun)
    fake_run.entity_type = ReconciliationEntityType.access_fact
    fake_run.id = run_id
    fake_run.application_id = app_id
    fake_run.status = ReconciliationRunStatus.pending_apply

    items = [_make_fake_delta_item(run_id) for _ in range(3)]

    test_app = _make_test_app(session_factory=session_factory)

    with (
        patch(
            'src.engines.inventory_reconcile.routes.get_run',
            new=AsyncMock(return_value=fake_run),
        ),
        patch(
            'src.engines.inventory_reconcile.routes.list_delta_items',
            new=AsyncMock(return_value=items),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.get(f'/inventory-reconciles/runs/{run_id}/delta-items')

    assert resp.status_code == 200
    body = resp.json()
    assert len(body['items']) == 3
    assert body['next_cursor'] is None


@pytest.mark.asyncio
async def test_get_delta_items_cursor_pagination(session_factory):
    """GET delta-items: when limit+1 rows returned, next_cursor is set."""
    run_id = uuid4()
    limit = 2

    from src.engines.inventory_reconcile.models import (
        ReconciliationEntityType,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    fake_run = MagicMock(spec=ReconciliationRun)
    fake_run.entity_type = ReconciliationEntityType.access_fact
    fake_run.id = run_id
    fake_run.application_id = uuid4()
    fake_run.status = ReconciliationRunStatus.pending_apply

    # Return limit+1 items to signal there are more pages
    items = [_make_fake_delta_item(run_id) for _ in range(limit + 1)]

    test_app = _make_test_app(session_factory=session_factory)

    with (
        patch(
            'src.engines.inventory_reconcile.routes.get_run',
            new=AsyncMock(return_value=fake_run),
        ),
        patch(
            'src.engines.inventory_reconcile.routes.list_delta_items',
            new=AsyncMock(return_value=items),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.get(
                f'/inventory-reconciles/runs/{run_id}/delta-items',
                params={'limit': limit},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body['items']) == limit  # only limit items returned
    assert body['next_cursor'] is not None


@pytest.mark.asyncio
async def test_get_delta_items_cursor_roundtrip(session_factory):
    """Cursor from page 1 can be decoded and passed to page 2 without error."""
    run_id = uuid4()
    limit = 1

    from src.engines.inventory_reconcile.models import (
        ReconciliationEntityType,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    fake_run = MagicMock(spec=ReconciliationRun)
    fake_run.entity_type = ReconciliationEntityType.access_fact
    fake_run.id = run_id
    fake_run.application_id = uuid4()
    fake_run.status = ReconciliationRunStatus.pending_apply

    items_page1 = [_make_fake_delta_item(run_id), _make_fake_delta_item(run_id)]
    items_page2 = [_make_fake_delta_item(run_id)]

    test_app = _make_test_app(session_factory=session_factory)

    call_count = 0

    async def mock_list(*args, **kwargs):
        nonlocal call_count
        if call_count == 0:
            call_count += 1
            return items_page1  # limit+1 = 2 items → has next
        return items_page2  # page 2 has 1 item → no next

    with (
        patch(
            'src.engines.inventory_reconcile.routes.get_run',
            new=AsyncMock(return_value=fake_run),
        ),
        patch(
            'src.engines.inventory_reconcile.routes.list_delta_items',
            new=AsyncMock(side_effect=mock_list),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp1 = await client.get(
                f'/inventory-reconciles/runs/{run_id}/delta-items',
                params={'limit': limit},
            )
            assert resp1.status_code == 200
            cursor = resp1.json()['next_cursor']
            assert cursor is not None

            resp2 = await client.get(
                f'/inventory-reconciles/runs/{run_id}/delta-items',
                params={'limit': limit, 'cursor': cursor},
            )

    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2['items']) == 1
    assert body2['next_cursor'] is None


@pytest.mark.asyncio
async def test_get_delta_items_unknown_run_returns_404(session_factory):
    """GET /runs/{id}/delta-items with unknown run id → 404."""
    run_id = uuid4()

    test_app = _make_test_app(session_factory=session_factory)

    with patch(
        'src.engines.inventory_reconcile.routes.get_run',
        new=AsyncMock(return_value=None),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.get(f'/inventory-reconciles/runs/{run_id}/delta-items')

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_delta_items_status_filter(session_factory):
    """GET /delta-items?status=pending passes status to list_delta_items."""
    run_id = uuid4()

    from src.engines.inventory_reconcile.models import (
        ReconciliationDeltaItemStatus,
        ReconciliationEntityType,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    fake_run = MagicMock(spec=ReconciliationRun)
    fake_run.entity_type = ReconciliationEntityType.access_fact
    fake_run.id = run_id
    fake_run.application_id = uuid4()
    fake_run.status = ReconciliationRunStatus.pending_apply

    mock_list = AsyncMock(return_value=[])

    test_app = _make_test_app(session_factory=session_factory)

    with (
        patch(
            'src.engines.inventory_reconcile.routes.get_run',
            new=AsyncMock(return_value=fake_run),
        ),
        patch(
            'src.engines.inventory_reconcile.routes.list_delta_items',
            new=mock_list,
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.get(
                f'/inventory-reconciles/runs/{run_id}/delta-items',
                params={'status': 'pending'},
            )

    assert resp.status_code == 200
    call_kwargs = mock_list.call_args.kwargs
    assert call_kwargs['status'] == ReconciliationDeltaItemStatus.pending
