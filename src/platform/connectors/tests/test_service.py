# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ConnectorInstanceService."""

import pytest
from src.platform.connectors.service import ConnectorInstanceService
from src.platform.connectors.tests.support import mark_connector_instance_offline
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent, LogParticipantKind
from src.platform.logs.service import LogService


@pytest.fixture
def service() -> ConnectorInstanceService:
    return ConnectorInstanceService()


@pytest.mark.asyncio
async def test_upsert_instance_creates_row(
    service: ConnectorInstanceService,
    session_factory,
) -> None:
    async with session_factory() as session:
        instance = await service.upsert_instance(
            session,
            instance_id='conn-inst-1',
            tags=['jira', 'eu-segment'],
        )
        await session.commit()

    assert instance.id is not None
    assert instance.instance_id == 'conn-inst-1'
    assert instance.tags == ['jira', 'eu-segment']
    assert instance.is_online is True


@pytest.mark.asyncio
async def test_upsert_instance_updates_existing_row(
    service: ConnectorInstanceService,
    session_factory,
) -> None:
    async with session_factory() as session:
        await service.upsert_instance(
            session,
            instance_id='conn-inst-1',
            tags=['jira'],
        )
        await session.commit()

    async with session_factory() as session:
        instance = await service.upsert_instance(
            session,
            instance_id='conn-inst-1',
            tags=['jira', 'eu-segment'],
        )
        await mark_connector_instance_offline(session, 'conn-inst-1')
        await session.refresh(instance)
        await session.commit()

    assert instance.instance_id == 'conn-inst-1'
    assert instance.tags == ['jira', 'eu-segment']
    assert instance.is_online is False


@pytest.mark.asyncio
async def test_get_instance_returns_row(
    service: ConnectorInstanceService,
    session_factory,
) -> None:
    async with session_factory() as session:
        await service.upsert_instance(
            session,
            instance_id='conn-inst-2',
            tags=['jira'],
        )
        await session.commit()

    async with session_factory() as session:
        instance = await service.get_instance(session, 'conn-inst-2')

    assert instance is not None
    assert instance.instance_id == 'conn-inst-2'
    assert instance.tags == ['jira']


@pytest.mark.asyncio
async def test_list_instances_returns_all_rows(
    service: ConnectorInstanceService,
    session_factory,
) -> None:
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
        await session.commit()

    async with session_factory() as session:
        instances = await service.list_instances(session)

    assert [i.instance_id for i in instances] == ['conn-inst-a', 'conn-inst-b']


@pytest.mark.asyncio
async def test_register_from_message_emits_capability_actor_and_connector_target(
    service: ConnectorInstanceService,
    session_factory,
) -> None:
    captured: list[LogEvent] = []

    class _CapSink(LogSink):
        def emit(self, event: LogEvent) -> None:
            captured.append(event)

    factory = LogSinkFactory()
    factory.register('cap', lambda: _CapSink())
    log = LogService(sink=factory.get('cap'))

    async with session_factory() as session:
        await service.register_from_message(
            session,
            instance_id='conn-participant-1',
            tags=['jira'],
            log_service=log,
        )
        await session.commit()

    # Step 23: event_type no longer forwarded via emit_safe; filter by message instead.
    reg_events = [e for e in captured if e.message == 'Connector instance registered']
    assert len(reg_events) == 1
    ev = reg_events[0]
    assert ev.initiator_id == 'connectors'
    assert ev.actor_id == 'connectors'
    assert ev.target_type == LogParticipantKind.CONNECTOR
    assert ev.target_id == 'conn-participant-1'
