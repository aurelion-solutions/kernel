# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for PATCH /applications/{id}."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_patch_application_updates_config(session_factory, app):
    async with session_factory() as session:
        row = Application(
            name='patch-config-app',
            code='patch-config-app',
            config={'version': 1},
            required_connector_tags=['t1'],
        )
        session.add(row)
        await session.commit()
        app_id = row.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/applications/{app_id}',
            json={'config': {'version': 2, 'flag': True}},
        )

    assert response.status_code == 200
    data = response.json()
    assert data['name'] == 'patch-config-app'
    assert data['code'] == 'patch-config-app'
    assert data['config'] == {'version': 2, 'flag': True}
    assert data['required_connector_tags'] == ['t1']


@pytest.mark.asyncio
async def test_patch_application_returns_404_when_missing(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/applications/{uuid.uuid4()}',
            json={'config': {}},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_application_rejects_empty_body(session_factory, app):
    async with session_factory() as session:
        row = Application(name='empty-patch-app', code='empty-patch-app', config={}, required_connector_tags=[])
        session.add(row)
        await session.commit()
        app_id = row.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/applications/{app_id}',
            json={},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_patch_application_can_change_code(session_factory, app):
    """PATCH with code='ad-prod' changes the code."""
    async with session_factory() as session:
        row = Application(name='change-code-app', code='change-code-app', config={})
        session.add(row)
        await session.commit()
        app_id = row.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/applications/{app_id}',
            json={'code': 'ad-prod'},
        )

    assert response.status_code == 200
    data = response.json()
    assert data['code'] == 'ad-prod'


@pytest.mark.asyncio
async def test_patch_application_duplicate_code_returns_409(session_factory, app):
    """PATCH to a code already owned by another app returns 409."""
    async with session_factory() as session:
        app1 = Application(name='app-ad', code='ad', config={})
        app2 = Application(name='app-jira', code='jira', config={})
        session.add_all([app1, app2])
        await session.commit()
        app2_id = app2.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/applications/{app2_id}',
            json={'code': 'ad'},
        )

    assert response.status_code == 409
