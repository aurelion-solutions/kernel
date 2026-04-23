# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for POST /reconciliation/runs."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.capabilities.reconciliation.deps import get_reconciliation_service
from src.capabilities.reconciliation.routes import router as reconciliation_router
from src.capabilities.reconciliation.schemas import ReconciliationRunSummary
from src.core.db.deps import get_db
from src.platform.applications.exceptions import ApplicationNotFoundError


def _make_summary(app_id: uuid.UUID) -> ReconciliationRunSummary:
    return ReconciliationRunSummary(
        application_id=app_id,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        artifacts_ingested=1,
        facts_created=1,
        facts_updated=0,
        facts_revoked=0,
        artifacts_unhandled=0,
        facts_errored=0,
    )


@pytest.mark.asyncio
async def test_post_run_happy_path(session_factory):
    """POST /reconciliation/runs returns 200 + ReconciliationRunSummary JSON."""
    app_id = uuid.uuid4()
    summary = _make_summary(app_id)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    test_app = FastAPI()
    test_app.include_router(reconciliation_router)
    test_app.dependency_overrides[get_db] = override_get_db

    with patch(
        'src.capabilities.reconciliation.routes.get_reconciliation_service',
    ) as mock_factory:
        mock_svc = AsyncMock()
        mock_svc.run = AsyncMock(return_value=summary)
        mock_factory.return_value = mock_svc

        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.post(
                '/reconciliation/runs',
                json={'application_id': str(app_id)},
            )

    assert resp.status_code == 200
    body = resp.json()
    for field in (
        'application_id',
        'started_at',
        'finished_at',
        'artifacts_ingested',
        'facts_created',
        'facts_updated',
        'facts_revoked',
        'artifacts_unhandled',
    ):
        assert field in body


@pytest.mark.asyncio
async def test_post_run_application_not_found_404(session_factory):
    """POST /reconciliation/runs with unknown application_id → 404."""

    async def override_get_db():
        async with session_factory() as session:
            yield session

    test_app = FastAPI()
    test_app.include_router(reconciliation_router)
    test_app.dependency_overrides[get_db] = override_get_db

    with patch(
        'src.capabilities.reconciliation.routes.get_reconciliation_service',
    ) as mock_factory:
        mock_svc = AsyncMock()
        mock_svc.run = AsyncMock(side_effect=ApplicationNotFoundError('not found'))
        mock_factory.return_value = mock_svc

        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url='http://testserver',
        ) as client:
            resp = await client.post(
                '/reconciliation/runs',
                json={'application_id': str(uuid.uuid4())},
            )

    assert resp.status_code == 404
    assert 'Application not found' in resp.json()['detail']


@pytest.mark.asyncio
async def test_post_run_unknown_application_id_returns_404_real_db(session_factory):
    """POST /reconciliation/runs with random UUID → 404 via real DB path (no service mock)."""

    async def override_get_db():
        async with session_factory() as session:
            yield session

    def override_get_svc(session):
        return get_reconciliation_service(session)

    test_app = FastAPI()
    test_app.include_router(reconciliation_router)
    test_app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/reconciliation/runs',
            json={'application_id': str(uuid.uuid4())},
        )

    assert resp.status_code == 404
    assert 'Application not found' in resp.json()['detail']
