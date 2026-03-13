# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import pytest
from src.platform.connectors.service import ConnectorInstanceService


@pytest.mark.asyncio
async def test_get_connector_instance_returns_row(
    client,
    session_factory,
) -> None:
    service = ConnectorInstanceService()

    async with session_factory() as session:
        await service.upsert_instance(
            session,
            instance_id='conn-inst-1',
            tags=['jira', 'eu-segment'],
        )
        await session.commit()

    response = await client.get('/api/v0/connector-instances/conn-inst-1')

    assert response.status_code == 200
    data = response.json()

    assert data['instance_id'] == 'conn-inst-1'
    assert data['tags'] == ['jira', 'eu-segment']
    assert data['is_online'] is True


@pytest.mark.asyncio
async def test_get_connector_instance_returns_404_for_unknown_instance(
    client,
) -> None:
    response = await client.get('/api/v0/connector-instances/no-such-connector-instance')

    assert response.status_code == 404
    assert response.json() == {'detail': 'Connector instance not found'}
