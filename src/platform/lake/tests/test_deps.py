# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/platform/lake/deps.py."""

from typing import Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink


def make_test_app() -> tuple[FastAPI, LakeSessionFactory]:
    sink = CapturingLogSink()
    log_service = LogService(sink=sink)
    settings = LakeSettings(
        catalog_url='sqlite:///test_catalog_deps.db',
        warehouse_uri='file:///tmp/test_warehouse_deps',
        pool_size=2,
        acquire_timeout_seconds=5.0,
    )
    factory = LakeSessionFactory(settings=settings, log_service=log_service, pg_dsn=None)

    app = FastAPI()
    app.state.lake_session_factory = factory

    @app.get('/_t')
    async def _test_route(s: LakeSession = Depends(get_lake_session)) -> dict[str, Any]:  # noqa: B008
        row = s.execute('SELECT 1').fetchone()
        return {'pool_open': factory._open_count, 'result': row[0] if row else None}

    return app, factory


def test_dep_yields_session_and_releases() -> None:
    app, factory = make_test_app()
    client = TestClient(app)
    response = client.get('/_t')
    assert response.status_code == 200
    data = response.json()
    assert data['result'] == 1
    assert factory._open_count == 1
    # Pool queue should have the session back.
    assert factory._pool.qsize() == 1
    factory.close_all()


def test_dep_no_leaks_on_sequential_requests() -> None:
    app, factory = make_test_app()
    client = TestClient(app)
    for _ in range(5):
        response = client.get('/_t')
        assert response.status_code == 200

    # open_count stays at 1 — pool reuses the same connection.
    assert factory._open_count == 1
    factory.close_all()


def test_dep_releases_on_handler_exception() -> None:
    sink = CapturingLogSink()
    log_service = LogService(sink=sink)
    settings = LakeSettings(
        catalog_url='sqlite:///test_catalog_exc.db',
        warehouse_uri='file:///tmp/test_warehouse_exc',
        pool_size=1,
        acquire_timeout_seconds=5.0,
    )
    factory = LakeSessionFactory(settings=settings, log_service=log_service, pg_dsn=None)
    app = FastAPI()
    app.state.lake_session_factory = factory

    @app.get('/_err')
    async def _err_route(s: LakeSession = Depends(get_lake_session)) -> dict[str, Any]:  # noqa: B008
        raise ValueError('intentional test error')

    client = TestClient(app, raise_server_exceptions=False)
    client.get('/_err')

    # Session must be back in the pool after an exception.
    assert factory._pool.qsize() == 1
    factory.close_all()
