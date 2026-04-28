# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route tests for POST /reconciliation/runs/{id}/apply."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.capabilities.sync_apply.deps import get_sync_apply_service
from src.capabilities.sync_apply.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyRunNotFoundError,
)
from src.capabilities.sync_apply.models import SyncApplyRunStatus
from src.capabilities.sync_apply.routes import router as sync_apply_router
from src.capabilities.sync_apply.schemas import SyncApplyApplyResponse
from src.core.db.deps import get_db

_NOW = datetime.now(tz=UTC)
_APP_ID = uuid4()
_SUBJECT_ID = uuid4()
_RESOURCE_ID = uuid4()


def _after_json() -> dict:
    return {
        'effect': 'allow',
        'valid_from': _NOW.isoformat(),
        'observed_at': _NOW.isoformat(),
        'created_at': _NOW.isoformat(),
        'valid_until': None,
        'revoked_at': None,
        'latest_batch_id': None,
    }


def _make_test_app(session_factory=None, mock_service=None) -> FastAPI:
    test_app = FastAPI()
    test_app.include_router(sync_apply_router)

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
        test_app.dependency_overrides[get_sync_apply_service] = _make_override(mock_service)
    else:
        # Always override DI to avoid needing real lake infra in route tests
        default_svc = AsyncMock()
        default_svc.apply = AsyncMock(side_effect=RuntimeError('No mock service configured'))
        test_app.dependency_overrides[get_sync_apply_service] = _make_override(default_svc)

    return test_app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_happy_path(session_factory) -> None:
    """POST /reconciliation/runs/{id}/apply with mode=auto_apply → 200 with applied_count=2."""
    run_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.apply = AsyncMock(
        return_value=SyncApplyApplyResponse(
            apply_run_id=uuid4(),
            status=SyncApplyRunStatus.completed,
            applied_count=2,
            failed_count=0,
            snapshot_ids={'create': 12345},
        )
    )

    test_app = _make_test_app(session_factory=session_factory, mock_service=mock_svc)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            f'/reconciliation/runs/{run_id}/apply',
            json={'mode': 'auto_apply'},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body['applied_count'] == 2
    assert body['failed_count'] == 0
    assert body['status'] == 'completed'
    assert body['snapshot_ids'] == {'create': 12345}


@pytest.mark.asyncio
async def test_apply_dry_run_response_shape(session_factory) -> None:
    """POST with mode=dry_run → applied_count=0, snapshot_ids={}."""
    run_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.apply = AsyncMock(
        return_value=SyncApplyApplyResponse(
            apply_run_id=uuid4(),
            status=SyncApplyRunStatus.completed,
            applied_count=0,
            failed_count=0,
            snapshot_ids={},
        )
    )

    test_app = _make_test_app(session_factory=session_factory, mock_service=mock_svc)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            f'/reconciliation/runs/{run_id}/apply',
            json={'mode': 'dry_run'},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body['applied_count'] == 0
    assert body['status'] == 'completed'
    assert body['snapshot_ids'] == {}


@pytest.mark.asyncio
async def test_apply_unknown_run_404(session_factory) -> None:
    """POST with unknown run_id → 404."""
    run_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.apply = AsyncMock(side_effect=SyncApplyRunNotFoundError(run_id))

    test_app = _make_test_app(session_factory=session_factory, mock_service=mock_svc)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            f'/reconciliation/runs/{run_id}/apply',
            json={'mode': 'auto_apply'},
        )

    assert resp.status_code == 404
    assert 'not found' in resp.json()['detail'].lower()


@pytest.mark.asyncio
async def test_apply_invalid_mode_422(session_factory) -> None:
    """POST with invalid mode → 422 (Pydantic validation)."""
    run_id = uuid4()

    test_app = _make_test_app(session_factory=session_factory)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            f'/reconciliation/runs/{run_id}/apply',
            json={'mode': 'wat'},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_apply_already_executed_409(session_factory) -> None:
    """Second POST to the same run → 409."""
    run_id = uuid4()

    mock_svc = AsyncMock()
    mock_svc.apply = AsyncMock(side_effect=SyncApplyAlreadyExecutedError(run_id))

    test_app = _make_test_app(session_factory=session_factory, mock_service=mock_svc)

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            f'/reconciliation/runs/{run_id}/apply',
            json={'mode': 'manual_apply'},
        )

    assert resp.status_code == 409
    assert 'already' in resp.json()['detail'].lower()
