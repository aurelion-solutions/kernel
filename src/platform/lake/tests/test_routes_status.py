# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP-layer tests for GET /api/v0/lake/status."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_catalog, get_lake_settings
from src.platform.lake.read_schemas import LakeStatusResponse
from src.platform.lake.routes import router as lake_router
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(catalog_override: Any, settings_override: LakeSettings) -> FastAPI:
    """Build a minimal FastAPI app with the lake router and dependency overrides."""
    app = FastAPI()
    app.include_router(lake_router, prefix='/api/v0')
    app.dependency_overrides[get_lake_catalog] = lambda: catalog_override
    app.dependency_overrides[get_lake_settings] = lambda: settings_override
    app.dependency_overrides[get_log_service] = lambda: NoOpLogService()
    return app


def _make_settings() -> LakeSettings:
    return LakeSettings(
        catalog_url='sqlite:///test.db',
        warehouse_uri='file:///tmp/warehouse',
        storage_provider='file',
    )


def _make_mock_catalog(namespaces: list[tuple[str, ...]], tables_by_ns: dict) -> Any:
    """Build a MagicMock Catalog that returns the given namespaces and tables."""
    catalog = MagicMock()
    catalog.list_namespaces.return_value = namespaces

    def _list_tables(ns: tuple[str, ...]) -> list[tuple[str, ...]]:
        return tables_by_ns.get(ns, [])

    catalog.list_tables.side_effect = _list_tables

    def _load_table(identifier: tuple[str, ...]) -> Any:
        tbl = MagicMock()
        tbl.name.return_value = identifier
        tbl.metadata.current_snapshot_id = None
        tbl.metadata.snapshots = []
        tbl.current_snapshot.return_value = None
        return tbl

    catalog.load_table.side_effect = _load_table
    return catalog


# ---------------------------------------------------------------------------
# T1: tables present → 200 + valid LakeStatusResponse shape
# ---------------------------------------------------------------------------


def test_get_status_returns_200_and_shape() -> None:
    """GET /api/v0/lake/status with tables returns 200 and validates against LakeStatusResponse."""
    namespaces = [('raw',), ('normalized',)]
    tables_by_ns: dict[tuple[str, ...], list[tuple[str, ...]]] = {
        ('raw',): [('raw', 'access_artifacts')],
        ('normalized',): [('normalized', 'access_facts')],
    }
    catalog = _make_mock_catalog(namespaces, tables_by_ns)
    settings = _make_settings()
    app = _make_app(catalog, settings)

    with TestClient(app) as client:
        resp = client.get('/api/v0/lake/status')

    assert resp.status_code == 200
    body = resp.json()
    response = LakeStatusResponse.model_validate(body)
    assert len(response.tables) == 2
    assert response.storage_provider == 'file'
    assert response.warehouse_uri == 'file:///tmp/warehouse'


# ---------------------------------------------------------------------------
# T2: empty catalog → 200 + tables == []
# ---------------------------------------------------------------------------


def test_get_status_handles_empty_catalog() -> None:
    """GET /api/v0/lake/status with empty catalog returns 200 and tables == []."""
    catalog = _make_mock_catalog(namespaces=[], tables_by_ns={})
    settings = _make_settings()
    app = _make_app(catalog, settings)

    with TestClient(app) as client:
        resp = client.get('/api/v0/lake/status')

    assert resp.status_code == 200
    body = resp.json()
    assert body['tables'] == []
