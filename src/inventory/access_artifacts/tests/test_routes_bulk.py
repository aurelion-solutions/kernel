# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessArtifact bulk API routes (Phase 15 Step 16 — iceberg-only)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.access_artifacts.deps import get_access_artifact_service
from src.inventory.access_artifacts.routes import router as access_artifacts_router
from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.inventory.access_artifacts.service import (
    AccessArtifactBatchTooLargeError,
    AccessArtifactLakeNotConfiguredError,
    AccessArtifactService,
    ArtifactCursorPage,
    BatchTombstoneResult,
    BatchUpsertResult,
    _encode_cursor,
)
from src.platform.lake.deps import get_lake_session, get_optional_lake_session
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_capturing_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


def _make_noop_lake_session() -> MagicMock:
    return MagicMock(spec=LakeSession)


def _make_view(
    *,
    artifact_type: str = 'sap_role',
    external_id: str | None = None,
    app_id: uuid.UUID | None = None,
) -> AccessArtifactView:
    now = datetime.now(UTC)
    return AccessArtifactView(
        id=uuid.uuid4(),
        application_id=app_id or uuid.uuid4(),
        artifact_type=artifact_type,
        external_id=external_id or f'ext-{uuid.uuid4().hex[:8]}',
        payload={'name': 'Test Role'},
        raw_name=None,
        effect=None,
        valid_from=None,
        valid_until=None,
        is_active=True,
        tombstoned_at=None,
        observed_at=now,
        ingested_at=now,
        ingest_batch_id=None,
    )


def _make_app(service_override: AccessArtifactService | None = None) -> FastAPI:
    """Build a FastAPI app with access_artifacts router and optional service override."""

    async def override_get_optional_lake_session() -> Any:
        yield _make_noop_lake_session()

    async def override_get_lake_session() -> Any:
        yield _make_noop_lake_session()

    app = FastAPI()
    app.include_router(access_artifacts_router, prefix='/api/v0')
    app.dependency_overrides[get_optional_lake_session] = override_get_optional_lake_session
    app.dependency_overrides[get_lake_session] = override_get_lake_session

    if service_override is not None:
        app.dependency_overrides[get_access_artifact_service] = lambda: service_override

    return app


def _make_bulk_item(
    app_id: uuid.UUID,
    artifact_type: str = 'sap_role',
    external_id: str | None = None,
) -> dict:
    return {
        'application_id': str(app_id),
        'artifact_type': artifact_type,
        'external_id': external_id or f'ext-{uuid.uuid4().hex[:8]}',
        'payload': {'name': 'Test Role'},
    }


# ---------------------------------------------------------------------------
# Test 1: bulk upsert iceberg — returns counts and snapshot_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_iceberg_returns_counts_and_snapshot() -> None:
    """POST /bulk with iceberg backend (mocked service) returns snapshot_id."""
    mock_svc = MagicMock(spec=AccessArtifactService)
    mock_svc.upsert_batch = AsyncMock(return_value=BatchUpsertResult(row_count=3, snapshot_id=42, backend='iceberg'))
    app = _make_app(service_override=mock_svc)

    app_id = uuid.uuid4()
    batch_id = str(uuid.uuid4())
    items = [_make_bulk_item(app_id, external_id=f'role-ice-{i}') for i in range(3)]

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.post(
            '/api/v0/access-artifacts/bulk',
            json={'ingest_batch_id': batch_id, 'items': items},
        )

    assert response.status_code == 200
    data = response.json()
    assert data['row_count'] == 3
    assert data['snapshot_id'] == 42
    assert data['backend'] == 'iceberg'


# ---------------------------------------------------------------------------
# Test 2: bulk tombstone returns counts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_tombstone_returns_counts() -> None:
    """POST /bulk-tombstone returns tombstoned_count."""
    mock_svc = MagicMock(spec=AccessArtifactService)
    mock_svc.tombstone_batch = AsyncMock(
        return_value=BatchTombstoneResult(row_count=2, snapshot_id=None, backend='iceberg')
    )
    app = _make_app(service_override=mock_svc)

    artifact_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.post(
            '/api/v0/access-artifacts/bulk-tombstone',
            json={
                'artifact_ids': artifact_ids,
                'observed_at': datetime.now(UTC).isoformat(),
            },
        )

    assert response.status_code == 200
    data = response.json()
    assert data['tombstoned_count'] == 2
    assert data['backend'] == 'iceberg'


# ---------------------------------------------------------------------------
# Test 3: bulk upsert too large — 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_too_large_returns_422() -> None:
    """POST /bulk with 10 001 items returns 422 (Pydantic max_length)."""
    mock_svc = MagicMock(spec=AccessArtifactService)
    mock_svc.upsert_batch = AsyncMock(side_effect=AccessArtifactBatchTooLargeError(10_001, 10_000))
    app = _make_app(service_override=mock_svc)

    app_id = uuid.uuid4()
    batch_id = str(uuid.uuid4())
    items = [_make_bulk_item(app_id, external_id=f'r-{i}') for i in range(10_001)]

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.post(
            '/api/v0/access-artifacts/bulk',
            json={'ingest_batch_id': batch_id, 'items': items},
        )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Test 4: bulk upsert missing lake config — 503
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_missing_lake_config_returns_503() -> None:
    """POST /bulk when service raises AccessArtifactLakeNotConfiguredError → 503."""
    mock_svc = MagicMock(spec=AccessArtifactService)
    mock_svc.upsert_batch = AsyncMock(side_effect=AccessArtifactLakeNotConfiguredError())
    app = _make_app(service_override=mock_svc)

    app_id = uuid.uuid4()
    batch_id = str(uuid.uuid4())
    items = [_make_bulk_item(app_id, external_id='role-x')]

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.post(
            '/api/v0/access-artifacts/bulk',
            json={'ingest_batch_id': batch_id, 'items': items},
        )

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Test 5: GET cursor pagination — iceberg backend (mocked service)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_artifacts_cursor_pagination() -> None:
    """GET /access-artifacts with iceberg backend paginates via cursor."""
    v1 = _make_view()
    v2 = _make_view()
    v3 = _make_view()
    v4 = _make_view()
    v5 = _make_view()

    page1_items = [v1, v2]
    page2_items = [v3, v4]
    page3_items = [v5]

    cursor_after_page1 = _encode_cursor(str(page1_items[-1].id))
    cursor_after_page2 = _encode_cursor(str(page2_items[-1].id))

    mock_svc = MagicMock(spec=AccessArtifactService)
    mock_svc._get_warehouse_uri.return_value = 'file:///tmp/test_warehouse'
    mock_svc._get_read_page_size.return_value = 2
    mock_svc.list_artifacts_iceberg = AsyncMock(
        side_effect=[
            ArtifactCursorPage(items=page1_items, next_cursor=cursor_after_page1),
            ArtifactCursorPage(items=page2_items, next_cursor=cursor_after_page2),
            ArtifactCursorPage(items=page3_items, next_cursor=None),
        ]
    )
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        r1 = await client.get('/api/v0/access-artifacts')
        assert r1.status_code == 200
        d1 = r1.json()
        assert len(d1['items']) == 2
        assert d1['next_cursor'] == cursor_after_page1

        r2 = await client.get('/api/v0/access-artifacts', params={'cursor': d1['next_cursor']})
        assert r2.status_code == 200
        d2 = r2.json()
        assert len(d2['items']) == 2
        assert d2['next_cursor'] == cursor_after_page2

        r3 = await client.get('/api/v0/access-artifacts', params={'cursor': d2['next_cursor']})
        assert r3.status_code == 200
        d3 = r3.json()
        assert len(d3['items']) == 1
        assert d3['next_cursor'] is None


# ---------------------------------------------------------------------------
# Test 6: GET invalid cursor — 400
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_artifacts_invalid_cursor_returns_400() -> None:
    """GET /access-artifacts with malformed cursor → 400."""
    mock_svc = MagicMock(spec=AccessArtifactService)
    mock_svc._get_warehouse_uri.return_value = 'file:///tmp/test_warehouse'
    mock_svc._get_read_page_size.return_value = 100
    mock_svc.list_artifacts_iceberg = AsyncMock(side_effect=AccessArtifactLakeNotConfiguredError())
    app = _make_app(service_override=mock_svc)

    from src.inventory.access_artifacts.service import InvalidCursorError

    mock_svc.list_artifacts_iceberg = AsyncMock(side_effect=InvalidCursorError())

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/access-artifacts', params={'cursor': 'not-base64!!!'})

    assert response.status_code == 400
