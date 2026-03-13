# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for DELETE /applications/{id}."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_delete_application_returns_204(session_factory, app):
    """Successful DELETE /applications/{id} returns 204 No Content."""
    async with session_factory() as session:
        app_model = Application(
            name='delete-route-app',
            code='delete-route-app',
            config={},
        )
        session.add(app_model)
        await session.commit()
        app_id = app_model.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.delete(f'/api/v0/applications/{app_id}')

    assert response.status_code == 204
    assert response.content == b''


@pytest.mark.asyncio
async def test_delete_application_not_found_returns_404(app):
    """Delete application not found returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.delete(f'/api/v0/applications/{uuid.uuid4()}')

    assert response.status_code == 404
    assert response.json()['detail'] == 'Application not found'
