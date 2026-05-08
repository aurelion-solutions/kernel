# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke tests for master data reconciliation routes.

These tests mock the pipeline and apply functions so no lake is required.
They verify HTTP contracts, status codes, and response schemas.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.engines.reconciliation.master_data_apply import MasterDataApplyResult
from src.engines.reconciliation.master_data_pipeline import MasterDataReconciliationResult
from src.engines.reconciliation.models import ReconciliationEntityType
from src.engines.reconciliation.routes import router
from src.platform.lake.deps import get_lake_session


def _make_test_app(session_factory, lake_session=None):
    app = FastAPI()
    app.include_router(router)

    async def _override_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_db
    if lake_session is not None:
        app.dependency_overrides[get_lake_session] = lambda: lake_session
    return app


def _make_pipeline_result(entity_type: ReconciliationEntityType, run_id: uuid.UUID):
    return MasterDataReconciliationResult(
        run_id=run_id,
        entity_type=entity_type,
        created_count=2,
        updated_count=1,
        revoked_count=0,
        unchanged_count=5,
    )


def _make_apply_result(entity_type: ReconciliationEntityType, run_id: uuid.UUID):
    return MasterDataApplyResult(
        run_id=run_id,
        entity_type=entity_type,
        applied_count=3,
        failed_count=0,
        ignored_count=0,
    )


@pytest.mark.asyncio
async def test_trigger_master_data_run_returns_pending_apply(session_factory):
    """POST /reconciliation/master-data/runs → 200 with status=pending_apply."""
    run_id = uuid.uuid4()

    fake_pipeline_result = _make_pipeline_result(ReconciliationEntityType.person, run_id)

    app = _make_test_app(session_factory, lake_session=object())

    with patch(
        'src.engines.reconciliation.routes.run_master_data_reconciliation',
        new=AsyncMock(return_value=fake_pipeline_result),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp = await client.post(
                '/reconciliation/master-data/runs',
                json={'entity_type': 'person'},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body['entity_type'] == 'person'
    assert body['status'] == 'pending_apply'
    assert body['created_count'] == 2


@pytest.mark.asyncio
async def test_trigger_master_data_run_does_not_call_apply(session_factory):
    """POST /master-data/runs must NOT call apply — apply is a separate step."""
    run_id = uuid.uuid4()
    fake_pipeline = _make_pipeline_result(ReconciliationEntityType.person, run_id)

    app = _make_test_app(session_factory, lake_session=object())

    mock_apply = AsyncMock()
    with (
        patch(
            'src.engines.reconciliation.routes.run_master_data_reconciliation',
            new=AsyncMock(return_value=fake_pipeline),
        ),
        patch(
            'src.engines.reconciliation.routes.apply_master_data_delta',
            new=mock_apply,
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp = await client.post(
                '/reconciliation/master-data/runs',
                json={'entity_type': 'person'},
            )

    assert resp.status_code == 200
    mock_apply.assert_not_called()


@pytest.mark.asyncio
async def test_trigger_invalid_entity_type_returns_422(session_factory):
    """POST with unknown entity_type → 422."""
    app = _make_test_app(session_factory, lake_session=object())

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        resp = await client.post(
            '/reconciliation/master-data/runs',
            json={'entity_type': 'unicorn', 'mode': 'dry_run'},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_apply_master_data_run_returns_200(session_factory):
    """POST /reconciliation/master-data/runs/{id}/apply → 200."""
    run_id = uuid.uuid4()
    fake_apply = _make_apply_result(ReconciliationEntityType.org_unit, run_id)

    app = _make_test_app(session_factory)

    with patch(
        'src.engines.reconciliation.routes.apply_master_data_delta',
        new=AsyncMock(return_value=fake_apply),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp = await client.post(
                f'/reconciliation/master-data/runs/{run_id}/apply',
                json={'entity_type': 'org_unit'},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body['entity_type'] == 'org_unit'
    assert body['applied_count'] == 3


@pytest.mark.asyncio
async def test_apply_run_not_found_returns_404(session_factory):
    """POST apply with unknown run_id → 404."""
    run_id = uuid.uuid4()
    app = _make_test_app(session_factory)

    with patch(
        'src.engines.reconciliation.routes.apply_master_data_delta',
        side_effect=LookupError(f'ReconciliationRun {run_id} not found'),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            resp = await client.post(
                f'/reconciliation/master-data/runs/{run_id}/apply',
                json={'entity_type': 'person'},
            )

    assert resp.status_code == 404
