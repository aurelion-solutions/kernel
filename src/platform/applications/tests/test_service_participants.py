# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Log participant fields for application lifecycle service."""

import pytest
from src.platform.applications.schemas import ApplicationCreate
from src.platform.applications.service import create_application
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent, LogParticipantKind
from src.platform.logs.service import LogService


@pytest.mark.asyncio
async def test_create_application_log_uses_applications_capability_and_application_target(
    session_factory,
) -> None:
    captured: list[LogEvent] = []

    class _CapSink(LogSink):
        def emit(self, event: LogEvent) -> None:
            captured.append(event)

    factory = LogSinkFactory()
    factory.register('cap', lambda: _CapSink())
    log = LogService(factory, provider_name='cap')

    async with session_factory() as session:
        app = await create_application(
            session,
            ApplicationCreate(name='trace-app', code='trace-app', config={'k': 1}, required_connector_tags=['jira']),
            log_service=log,
        )
        await session.commit()

    assert len(captured) == 1
    ev = captured[0]
    assert ev.event_type == 'application.created'
    assert ev.initiator_type == LogParticipantKind.CAPABILITY
    assert ev.initiator_id == 'applications'
    assert ev.actor_type == LogParticipantKind.CAPABILITY
    assert ev.actor_id == 'applications'
    assert ev.target_type == LogParticipantKind.APPLICATION
    assert ev.target_id == str(app.id)
