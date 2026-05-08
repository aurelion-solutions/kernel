# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke tests for POST /org-units/bulk (lake-first path)."""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.org_units.routes import router as org_units_router


@pytest.fixture
def app_no_lake() -> FastAPI:
    app = FastAPI()
    app.include_router(org_units_router)
    return app


@pytest.mark.asyncio
async def test_bulk_org_units_no_lake_returns_503(app_no_lake: FastAPI) -> None:
    """Without lake_catalog in app.state the endpoint returns 503."""
    payload = {'items': [{'external_id': 'OU1', 'name': 'Engineering'}]}
    async with AsyncClient(transport=ASGITransport(app=app_no_lake), base_url='http://test') as client:
        resp = await client.post('/org-units/bulk', json=payload)
    assert resp.status_code == 503
