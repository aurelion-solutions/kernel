# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from httpx import ASGITransport, AsyncClient
import pytest
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_list_applications_returns_200_and_array(session_factory, app):
    async with session_factory() as session:
        app1 = Application(
            name='app-a',
            code='app-a',
            config={},
            required_connector_tags=['jira'],
        )
        app2 = Application(
            name='app-b',
            code='app-b',
            config={'queue': 'q1'},
            required_connector_tags=['jira', 'eu-segment'],
        )
        session.add(app1)
        session.add(app2)
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/applications')

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 2
    names = [a['name'] for a in data]
    assert 'app-a' in names
    assert 'app-b' in names
    for item in data:
        assert 'id' in item
        assert 'name' in item
        assert 'code' in item
        assert 'config' in item
        assert 'required_connector_tags' in item
        assert 'is_active' in item
        assert 'created_at' in item
        assert 'updated_at' in item


@pytest.mark.asyncio
async def test_list_applications_returns_empty_array_when_none(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/applications')

    assert response.status_code == 200
    assert response.json() == []
