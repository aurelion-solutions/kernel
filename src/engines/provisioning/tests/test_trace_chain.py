# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end trace semantics for provisioning → connector command path."""

import pytest
from src.engines.provisioning.create_account import create_account
from src.engines.provisioning.schemas import AccountCreateRequest
from src.platform.applications.models import Application
from src.platform.connectors.service import ConnectorInstanceService
from src.platform.connectors.tests.support import RecordingStubRPCClient, connector_client_with_stub
from src.platform.logs.consumer import parse_connector_log_payload
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import (
    LogEvent,
    LogLevel,
    LogParticipantKind,
    new_downstream_log_event,
    new_downstream_log_event_from_parent_id,
    new_root_log_event,
)
from src.platform.logs.service import LogService


@pytest.mark.asyncio
async def test_create_account_emits_trace_and_invoke_carries_trace_context(session_factory) -> None:
    captured: list[LogEvent] = []

    class _CapSink(LogSink):
        def emit(self, event: LogEvent) -> None:
            captured.append(event)

    factory = LogSinkFactory()
    factory.register('cap', lambda: _CapSink())
    log = LogService(sink=factory.get('cap'))

    stub = RecordingStubRPCClient(
        {'create_account': {'status': 'ok', 'payload': {'username': 'alice', 'email': 'a@b.c'}}},
    )
    connector = connector_client_with_stub(stub)

    async with session_factory() as session:
        svc = ConnectorInstanceService()
        await svc.upsert_instance(
            session,
            instance_id='conn-trace-1',
            tags=['jira', 'eu'],
        )
        app = Application(
            name='trace-app',
            code='trace-app',
            required_connector_tags=['jira', 'eu'],
        )
        session.add(app)
        await session.commit()
        await session.refresh(app)
        app_id = app.id

        await create_account(
            session,
            app_id,
            AccountCreateRequest(username='alice', email='alice@example.org'),
            connector,
            log_service=log,
        )

    assert len(captured) == 2
    started, enqueued = captured
    assert started.event_type == 'provisioning.operation_started'
    assert started.causation_id is None
    assert started.initiator_type == LogParticipantKind.CAPABILITY
    assert started.actor_type == LogParticipantKind.CAPABILITY
    assert started.actor_id == 'provisioning'
    assert started.target_id == str(app_id)

    assert enqueued.event_type == 'connector.command.enqueued'
    assert enqueued.causation_id == started.event_id
    assert enqueued.correlation_id == started.correlation_id
    assert enqueued.actor_type == LogParticipantKind.CAPABILITY
    assert enqueued.target_type == LogParticipantKind.CONNECTOR
    assert enqueued.target_id == 'conn-trace-1'

    call = stub.calls[0]
    assert call['correlation_id'] == enqueued.correlation_id
    assert call['trace_parent_event_id'] == str(enqueued.event_id)
    assert call['trace_initiator_type'] == 'capability'
    assert call['trace_initiator_id'] == 'provisioning'
    assert call['trace_target_type'] == 'system'
    assert call['trace_target_id'] == str(app_id)


def test_simulated_connector_logs_chain_causation_and_preserves_initiator() -> None:
    """Mirror connector handler semantics: received → completed with preserved initiator/target."""
    started = new_root_log_event(
        event_type='provisioning.operation_started',
        level=LogLevel.INFO,
        message='s',
        component='provisioning',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='provisioning',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='provisioning',
        target_type=LogParticipantKind.SYSTEM,
        target_id='app-uuid',
        correlation_id='trace-corr-1',
        payload={},
    )
    enqueued = new_downstream_log_event(
        started,
        event_type='connector.command.enqueued',
        level=LogLevel.INFO,
        message='e',
        component='connector_client',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='provisioning',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='provisioning',
        target_type=LogParticipantKind.CONNECTOR,
        target_id='conn-1',
        payload={},
    )
    received = new_downstream_log_event_from_parent_id(
        parent_event_id=enqueued.event_id,
        correlation_id=enqueued.correlation_id,
        event_type='connector.command.received',
        level=LogLevel.INFO,
        message='r',
        component='connector',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='provisioning',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn-1',
        target_type=LogParticipantKind.SYSTEM,
        target_id='app-uuid',
        payload={},
    )
    completed = new_downstream_log_event(
        received,
        event_type='connector.command.completed',
        level=LogLevel.INFO,
        message='c',
        component='connector',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='provisioning',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn-1',
        target_type=LogParticipantKind.SYSTEM,
        target_id='app-uuid',
        payload={},
    )
    failed = new_downstream_log_event(
        received,
        event_type='connector.command.failed',
        level=LogLevel.ERROR,
        message='f',
        component='connector',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='provisioning',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn-1',
        target_type=LogParticipantKind.SYSTEM,
        target_id='app-uuid',
        payload={'error': 'x'},
    )

    assert completed.correlation_id == started.correlation_id
    assert completed.causation_id == received.event_id
    assert completed.initiator_id == 'provisioning'
    assert completed.actor_id == 'conn-1'
    assert completed.target_id == 'app-uuid'
    assert failed.causation_id == received.event_id
    assert failed.initiator_id == 'provisioning'

    for ev in (received, completed, failed):
        raw = ev.model_dump(mode='json')
        parsed = parse_connector_log_payload(raw)
        assert parsed is not None
        assert parsed.correlation_id == started.correlation_id
