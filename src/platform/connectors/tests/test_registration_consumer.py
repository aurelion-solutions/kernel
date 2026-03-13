# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import pytest
from src.platform.connectors.registration_consumer import handle_connector_registration
from src.platform.connectors.service import ConnectorInstanceService
from src.platform.connectors.tests.support import mark_connector_instance_offline


@pytest.mark.asyncio
async def test_handle_connector_registration_creates_instance(session_factory) -> None:
    message = {
        'event_type': 'connector.registered',
        'instance_id': 'conn-inst-1',
        'tags': ['jira', 'eu-segment'],
    }

    await handle_connector_registration(session_factory, message)

    service = ConnectorInstanceService()

    async with session_factory() as session:
        instance = await service.get_instance(session, 'conn-inst-1')

    assert instance is not None
    assert instance.instance_id == 'conn-inst-1'
    assert instance.tags == ['jira', 'eu-segment']
    assert instance.is_online is True


@pytest.mark.asyncio
async def test_handle_connector_registration_updates_instance(session_factory) -> None:
    service = ConnectorInstanceService()

    async with session_factory() as session:
        await service.upsert_instance(
            session,
            instance_id='conn-inst-1',
            tags=['jira'],
        )
        await mark_connector_instance_offline(session, 'conn-inst-1')
        await session.commit()

    message = {
        'event_type': 'connector.heartbeat',
        'instance_id': 'conn-inst-1',
        'tags': ['jira', 'eu-segment'],
    }

    await handle_connector_registration(session_factory, message)

    async with session_factory() as session:
        instance = await service.get_instance(session, 'conn-inst-1')

    assert instance is not None
    assert instance.instance_id == 'conn-inst-1'
    assert instance.tags == ['jira', 'eu-segment']
    assert instance.is_online is True
