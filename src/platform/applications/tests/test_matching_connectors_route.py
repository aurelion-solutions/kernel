# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for GET /applications/{id}/matching-connector-instances."""

from httpx import ASGITransport, AsyncClient
import pytest
from src.platform.applications.models import Application
from src.platform.connectors.models import ConnectorInstance


@pytest.mark.asyncio
async def test_matching_connector_instances_returns_200_and_filtered_list(
    session_factory,
    app,
):
    async with session_factory() as session:
        application = Application(
            name='api-match-app',
            code='api-match-app',
            required_connector_tags=['jira', 'eu'],
        )
        session.add(application)
        session.add(ConnectorInstance(instance_id='ok-1', tags=['jira', 'eu']))
        session.add(ConnectorInstance(instance_id='no-tags', tags=['jira']))
        await session.commit()
        app_id = application.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            f'/api/v0/applications/{app_id}/matching-connector-instances',
        )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    ids = {item['instance_id'] for item in data}
    assert 'ok-1' in ids
    assert 'no-tags' not in ids
    assert all('tags' in item and 'is_online' in item for item in data)


@pytest.mark.asyncio
async def test_matching_connector_instances_online_only_false_includes_offline(
    session_factory,
    app,
):
    from datetime import UTC, datetime, timedelta

    old = datetime.now(UTC) - timedelta(minutes=30)

    async with session_factory() as session:
        application = Application(
            name='api-match-offline',
            code='api-match-offline',
            required_connector_tags=['x'],
        )
        session.add(application)
        session.add(
            ConnectorInstance(
                instance_id='stale-x',
                tags=['x'],
                last_seen_at=old,
            ),
        )
        await session.commit()
        app_id = application.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        r_online = await client.get(
            f'/api/v0/applications/{app_id}/matching-connector-instances',
        )
        r_all = await client.get(
            f'/api/v0/applications/{app_id}/matching-connector-instances',
            params={'online_only': 'false'},
        )

    assert r_online.status_code == 200
    assert r_online.json() == []
    assert r_all.status_code == 200
    assert {item['instance_id'] for item in r_all.json()} == {'stale-x'}


@pytest.mark.asyncio
async def test_matching_connector_instances_returns_404_when_app_missing(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            '/api/v0/applications/00000000-0000-4000-8000-000000000001/matching-connector-instances',
        )

    assert response.status_code == 404
