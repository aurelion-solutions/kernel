# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import pytest
from src.platform.connectors.service import ConnectorInstanceService
from src.platform.connectors.tests.support import mark_connector_instance_offline


@pytest.mark.asyncio
async def test_list_connector_instances_returns_rows(
    client,
    session_factory,
) -> None:
    service = ConnectorInstanceService()

    async with session_factory() as session:
        await service.upsert_instance(
            session,
            instance_id='conn-inst-a',
            tags=['jira'],
        )
        await service.upsert_instance(
            session,
            instance_id='conn-inst-b',
            tags=['jira', 'eu-segment'],
        )
        await mark_connector_instance_offline(session, 'conn-inst-b')
        await session.commit()

    response = await client.get('/api/v0/connector-instances')

    assert response.status_code == 200
    data = response.json()

    assert [item['instance_id'] for item in data] == ['conn-inst-a', 'conn-inst-b']
    assert data[0]['tags'] == ['jira']
    assert data[0]['is_online'] is True
    assert data[1]['tags'] == ['jira', 'eu-segment']
    assert data[1]['is_online'] is False


@pytest.mark.asyncio
async def test_list_connector_instances_returns_empty_list(
    client,
) -> None:
    response = await client.get('/api/v0/connector-instances')

    assert response.status_code == 200
    assert response.json() == []
