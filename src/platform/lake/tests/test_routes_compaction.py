# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP-layer tests for POST /api/v0/lake/compaction."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.core.db.deps import get_db
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_catalog, get_lake_settings
from src.platform.lake.exceptions import LakeMaintenanceError
from src.platform.lake.maintenance import (
    CleanOrphanFilesResult,
    CompactTableResult,
    ExpireSnapshotsResult,
)
from src.platform.lake.routes import router as lake_router
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Canned results
# ---------------------------------------------------------------------------

_COMPACT_RESULT = CompactTableResult(
    files_before=10,
    files_after=2,
    bytes_before=1_000_000,
    bytes_after=950_000,
    snapshot_id=42,
)
_EXPIRE_RESULT = ExpireSnapshotsResult(snapshots_removed=3, latest_snapshot_id=42)
_CLEAN_RESULT = CleanOrphanFilesResult(files_removed=5, bytes_freed=200_000)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _make_session_dep(*, running_count: int = 0, batch_count: int = 0):
    """Return an async generator dependency that yields a mock AsyncSession."""
    call_index: list[int] = [0]
    counts = [running_count, batch_count]

    async def _dep() -> AsyncGenerator[Any]:
        session = MagicMock()

        async def _execute(stmt, *args, **kwargs):
            result = MagicMock()
            idx = call_index[0]
            result.scalar_one.return_value = counts[idx] if idx < len(counts) else 0
            call_index[0] += 1
            return result

        session.execute = _execute
        yield session

    return _dep


def _make_catalog() -> MagicMock:
    catalog = MagicMock()

    def _load(identifier: tuple[str, str]) -> MagicMock:
        tbl = MagicMock()
        tbl.name.return_value = identifier
        return tbl

    catalog.load_table.side_effect = _load
    return catalog


def _make_settings() -> LakeSettings:
    return LakeSettings(
        catalog_url='sqlite:///test.db',
        warehouse_uri='file:///tmp/warehouse',
        storage_provider='file',
    )


def _make_app(
    *,
    catalog: Any | None = None,
    session_dep=None,
) -> FastAPI:
    """Build a minimal FastAPI app with the lake router and dependency overrides."""
    app = FastAPI()
    app.include_router(lake_router, prefix='/api/v0')
    app.dependency_overrides[get_lake_catalog] = lambda: (catalog or _make_catalog())
    app.dependency_overrides[get_lake_settings] = lambda: _make_settings()
    app.dependency_overrides[get_log_service] = lambda: NoOpLogService()
    if session_dep is not None:
        app.dependency_overrides[get_db] = session_dep
    else:
        app.dependency_overrides[get_db] = _make_session_dep()
    return app


# ---------------------------------------------------------------------------
# T1: happy path — default body → 200, 2 tables, orphan_cleanup_skipped False
# ---------------------------------------------------------------------------


def test_happy_path_default_body() -> None:
    """POST /compaction with empty body returns 200, 2 tables, cleanup not skipped."""
    app = _make_app()

    with (
        patch('src.platform.lake.service.compact_table', return_value=_COMPACT_RESULT),
        patch('src.platform.lake.service.expire_old_snapshots', return_value=_EXPIRE_RESULT),
        patch('src.platform.lake.service.clean_orphan_files', return_value=_CLEAN_RESULT),
        TestClient(app) as client,
    ):
        resp = client.post('/api/v0/lake/compaction', json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body['orphan_cleanup_skipped'] is False
    assert body['orphan_cleanup_skip_reason'] is None
    assert len(body['tables']) == 2


# ---------------------------------------------------------------------------
# T2: validation — retention_days = 0 → 422
# ---------------------------------------------------------------------------


def test_validation_retention_days_zero() -> None:
    """retention_days=0 is below ge=1 → 422 with mention of retention_days."""
    app = _make_app()

    with TestClient(app) as client:
        resp = client.post('/api/v0/lake/compaction', json={'retention_days': 0})

    assert resp.status_code == 422
    assert 'retention_days' in resp.text


# ---------------------------------------------------------------------------
# T3: validation — unknown table value → 422
# ---------------------------------------------------------------------------


def test_validation_unknown_table() -> None:
    """table='garbage' is not a valid Literal → 422."""
    app = _make_app()

    with TestClient(app) as client:
        resp = client.post('/api/v0/lake/compaction', json={'table': 'garbage'})

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# T4: validation — orphan_older_than_hours = 0 → 422
# ---------------------------------------------------------------------------


def test_validation_orphan_hours_zero() -> None:
    """orphan_older_than_hours=0 is below ge=1 → 422."""
    app = _make_app()

    with TestClient(app) as client:
        resp = client.post('/api/v0/lake/compaction', json={'orphan_older_than_hours': 0})

    assert resp.status_code == 422
    assert 'orphan_older_than_hours' in resp.text


# ---------------------------------------------------------------------------
# T5: gate skipped — running_count=1 → 200, orphan_cleanup_skipped True
# ---------------------------------------------------------------------------


def test_gate_skipped_path() -> None:
    """When session returns running_count=1, orphan_cleanup is skipped in response."""
    app = _make_app(session_dep=_make_session_dep(running_count=1, batch_count=0))

    with (
        patch('src.platform.lake.service.compact_table', return_value=_COMPACT_RESULT),
        patch('src.platform.lake.service.expire_old_snapshots', return_value=_EXPIRE_RESULT),
        patch('src.platform.lake.service.clean_orphan_files', return_value=_CLEAN_RESULT),
        TestClient(app) as client,
    ):
        resp = client.post('/api/v0/lake/compaction', json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body['orphan_cleanup_skipped'] is True
    assert body['orphan_cleanup_skip_reason'] is not None


# ---------------------------------------------------------------------------
# T6: maintenance error → 500 with detail
# ---------------------------------------------------------------------------


def test_maintenance_error_returns_500() -> None:
    """LakeMaintenanceError from compact_table → 500 with detail containing the message."""
    app = _make_app()

    with (
        patch(
            'src.platform.lake.service.compact_table',
            side_effect=LakeMaintenanceError('boom'),
        ),
        TestClient(app) as client,
    ):
        resp = client.post('/api/v0/lake/compaction', json={})

    assert resp.status_code == 500
    assert 'boom' in resp.json()['detail']
