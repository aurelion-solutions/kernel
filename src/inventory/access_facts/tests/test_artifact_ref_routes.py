# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route tests for GET /access-facts/{fact_id}/artifact-ref."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.access_facts.deps import get_access_fact_service
from src.inventory.access_facts.routes import router as access_facts_router
from src.inventory.access_facts.schemas import AccessFactArtifactRefRead
from src.inventory.access_facts.service import AccessFactArtifactRefNotFoundError, AccessFactService
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession


def _make_app(service_override: AccessFactService | None = None) -> FastAPI:
    noop_lake = MagicMock(spec=LakeSession)

    async def override_get_lake_session():
        yield noop_lake

    app = FastAPI()
    app.include_router(access_facts_router, prefix='/api/v0')
    app.dependency_overrides[get_lake_session] = override_get_lake_session

    if service_override is not None:
        app.dependency_overrides[get_access_fact_service] = lambda: service_override

    return app


@pytest.mark.asyncio
async def test_get_artifact_ref_returns_200_and_shape() -> None:
    """Happy path: GET /access-facts/{fact_id}/artifact-ref returns 200 with correct shape."""
    fact_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    application_id = uuid.uuid4()
    external_id = 'ext-happy-001'

    ref = AccessFactArtifactRefRead(
        artifact_id=artifact_id,
        application_id=application_id,
        external_id=external_id,
    )

    mock_svc = MagicMock(spec=AccessFactService)
    mock_svc.get_artifact_ref = AsyncMock(return_value=ref)
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get(f'/api/v0/access-facts/{fact_id}/artifact-ref')

    assert response.status_code == 200
    data = response.json()
    assert data['artifact_id'] == str(artifact_id)
    assert data['application_id'] == str(application_id)
    assert data['external_id'] == external_id


@pytest.mark.asyncio
async def test_get_artifact_ref_returns_404_when_chain_broken() -> None:
    """Chain broken → GET /access-facts/{fact_id}/artifact-ref returns 404."""
    fact_id = uuid.uuid4()

    mock_svc = MagicMock(spec=AccessFactService)
    mock_svc.get_artifact_ref = AsyncMock(side_effect=AccessFactArtifactRefNotFoundError(fact_id))
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get(f'/api/v0/access-facts/{fact_id}/artifact-ref')

    assert response.status_code == 404
    data = response.json()
    assert data['detail'] == 'Access fact artifact reference not found'
