# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Log participant fields for connector result ingest."""

import uuid

import pytest
from src.capabilities.ingest.schemas import ConnectorResultIngestRequest
from src.capabilities.ingest.service import ingest_connector_result
from src.platform.applications.models import Application
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent, LogParticipantKind
from src.platform.logs.service import LogService


@pytest.mark.asyncio
async def test_ingest_success_log_uses_ingest_capability_and_application_target(session_factory) -> None:
    captured: list[LogEvent] = []

    class _CapSink(LogSink):
        def emit(self, event: LogEvent) -> None:
            captured.append(event)

    factory = LogSinkFactory()
    factory.register('cap', lambda: _CapSink())
    log = LogService(factory, provider_name='cap')

    async with session_factory() as session:
        app = Application(name='ingest-app', code='ingest-app', config={})
        session.add(app)
        await session.commit()
        await session.refresh(app)
        app_id = app.id

        req = ConnectorResultIngestRequest(
            task_id=str(uuid.uuid4()),
            application_id=str(app_id),
            operation='reconcile',
            status='completed',
            result_type='inline',
            result_id=str(uuid.uuid4()),
            payload={'ok': True},
        )
        await ingest_connector_result(session, req, log_service=log)
        await session.commit()

    assert len(captured) == 1
    ev = captured[0]
    assert ev.event_type == 'ingest.result.received'
    assert ev.initiator_id == 'ingest'
    assert ev.actor_id == 'ingest'
    assert ev.target_type == LogParticipantKind.APPLICATION
    assert ev.target_id == str(app_id)
