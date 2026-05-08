# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke tests for POST /employees/bulk (lake-first path)."""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.employees.routes import router as employees_router


@pytest.fixture
def app_no_lake() -> FastAPI:
    app = FastAPI()
    app.include_router(employees_router)
    return app


@pytest.mark.asyncio
async def test_bulk_employees_no_lake_returns_503(app_no_lake: FastAPI) -> None:
    """Without lake_catalog in app.state the endpoint returns 503."""
    payload = {'items': [{'person_external_id': 'P1'}]}
    async with AsyncClient(transport=ASGITransport(app=app_no_lake), base_url='http://test') as client:
        resp = await client.post('/employees/bulk', json=payload)
    assert resp.status_code == 503
