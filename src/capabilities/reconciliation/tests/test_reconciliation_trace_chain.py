# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end trace semantics for reconciliation → connector RPC path."""

from unittest.mock import patch

import pytest
from src.capabilities.reconciliation.orchestrator import (
    begin_reconciliation_trace,
    execute_reconciliation_continue,
    reconcile_application,
)
from src.platform.applications.models import Application
from src.platform.connectors.tests.support import (
    RecordingStubRPCClient,
    connector_client_with_stub,
    seed_online_connector_instance,
)
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
async def test_begin_then_execute_continue_shares_correlation_like_async_http_job(
    session_factory,
) -> None:
    """HTTP returns correlation_id from ``begin``; background job uses same root (reload app)."""
    captured: list[LogEvent] = []

    class _CapSink(LogSink):
        def emit(self, event: LogEvent) -> None:
            captured.append(event)

    factory = LogSinkFactory()
    factory.register('cap', lambda: _CapSink())
    log = LogService(factory, provider_name='cap')

    stub = RecordingStubRPCClient(
        {
            'list_accounts': {'status': 'ok', 'payload': {'accounts': []}},
            'list_roles': {'status': 'ok', 'payload': {'roles': []}},
            'list_privileges': {'status': 'ok', 'payload': {'privileges': []}},
        },
    )
    connector = connector_client_with_stub(stub)

    await seed_online_connector_instance(session_factory, instance_id='recon-async-path-conn')

    async with session_factory() as session:
        app = Application(
            name='recon-async-path-app',
            code='recon-async-path-app',
            config={'lake_provider': 'file'},
            required_connector_tags=[],
        )
        session.add(app)
        await session.commit()
        await session.refresh(app)
        app_id = app.id

    async with session_factory() as session:
        _app, instance_id, root = await begin_reconciliation_trace(session, app_id, log)
        expected_correlation_id = root.correlation_id
        await session.commit()

    async with session_factory() as session:
        await execute_reconciliation_continue(
            session,
            app_id,
            instance_id,
            connector,
            root,
            log,
            app=None,
        )
        await session.commit()

    assert len(captured) == 5
    root_ev, enq_a, enq_r, enq_p, completed_ev = captured
    assert root_ev.event_type == 'reconciliation.operation_started'
    assert completed_ev.event_type == 'reconciliation.completed'
    assert root_ev.correlation_id == expected_correlation_id
    for ev in captured:
        assert ev.correlation_id == expected_correlation_id
    for i, op in enumerate(('list_accounts', 'list_roles', 'list_privileges')):
        assert stub.calls[i]['operation'] == op
        assert stub.calls[i]['correlation_id'] == expected_correlation_id


@pytest.mark.asyncio
async def test_reconciliation_emits_trace_and_each_invoke_carries_trace_context(session_factory) -> None:
    captured: list[LogEvent] = []

    class _CapSink(LogSink):
        def emit(self, event: LogEvent) -> None:
            captured.append(event)

    factory = LogSinkFactory()
    factory.register('cap', lambda: _CapSink())
    log = LogService(factory, provider_name='cap')

    stub = RecordingStubRPCClient(
        {
            'list_accounts': {'status': 'ok', 'payload': {'accounts': []}},
            'list_roles': {'status': 'ok', 'payload': {'roles': []}},
            'list_privileges': {'status': 'ok', 'payload': {'privileges': []}},
        },
    )
    connector = connector_client_with_stub(stub)

    await seed_online_connector_instance(session_factory, instance_id='recon-trace-conn')

    async with session_factory() as session:
        app = Application(
            name='recon-trace-app',
            code='recon-trace-app',
            config={'lake_provider': 'file'},
            required_connector_tags=[],
        )
        session.add(app)
        await session.commit()
        await session.refresh(app)
        app_id = app.id

    async with session_factory() as session:
        await reconcile_application(session, app_id, connector, log_service=log)
        await session.commit()

    assert len(captured) == 5
    root, enq_accounts, enq_roles, enq_priv, completed_ev = captured
    assert completed_ev.event_type == 'reconciliation.completed'
    assert root.event_type == 'reconciliation.operation_started'
    assert root.causation_id is None
    assert root.initiator_id == 'reconciliation'
    assert root.actor_id == 'reconciliation'
    assert root.target_id == str(app_id)
    assert root.target_type == LogParticipantKind.APPLICATION

    assert enq_accounts.event_type == 'connector.command.enqueued'
    assert enq_accounts.causation_id == root.event_id
    assert enq_roles.causation_id == enq_accounts.event_id
    assert enq_priv.causation_id == enq_roles.event_id

    for e in (enq_accounts, enq_roles, enq_priv, completed_ev):
        assert e.correlation_id == root.correlation_id
    for e in (enq_accounts, enq_roles, enq_priv):
        assert e.initiator_id == 'reconciliation'
        assert e.actor_type == LogParticipantKind.CAPABILITY

    assert completed_ev.causation_id == enq_priv.event_id
    assert completed_ev.initiator_id == 'reconciliation'
    assert completed_ev.actor_id == 'reconciliation'
    assert completed_ev.target_id == str(app_id)
    assert completed_ev.target_type == LogParticipantKind.APPLICATION

    assert len(stub.calls) == 3
    for i, op in enumerate(('list_accounts', 'list_roles', 'list_privileges')):
        call = stub.calls[i]
        assert call['operation'] == op
        assert call['correlation_id'] == root.correlation_id
        assert call['trace_initiator_type'] == 'capability'
        assert call['trace_initiator_id'] == 'reconciliation'
        assert call['trace_target_type'] == 'application'
        assert call['trace_target_id'] == str(app_id)

    assert stub.calls[0]['trace_parent_event_id'] == str(enq_accounts.event_id)
    assert stub.calls[1]['trace_parent_event_id'] == str(enq_roles.event_id)
    assert stub.calls[2]['trace_parent_event_id'] == str(enq_priv.event_id)


@pytest.mark.asyncio
async def test_reconciliation_failed_shares_correlation_after_operation_started(
    session_factory,
) -> None:
    captured: list[LogEvent] = []

    class _CapSink(LogSink):
        def emit(self, event: LogEvent) -> None:
            captured.append(event)

    factory = LogSinkFactory()
    factory.register('cap', lambda: _CapSink())
    log = LogService(factory, provider_name='cap')

    stub = RecordingStubRPCClient(
        {
            'list_accounts': {'status': 'ok', 'payload': {'accounts': []}},
            'list_roles': {'status': 'ok', 'payload': {'roles': []}},
            'list_privileges': {'status': 'ok', 'payload': {'privileges': []}},
        },
    )
    connector = connector_client_with_stub(stub)

    await seed_online_connector_instance(session_factory, instance_id='recon-fail-conn')

    async with session_factory() as session:
        app = Application(
            name='recon-fail-app',
            code='recon-fail-app',
            config={'lake_provider': 'file'},
            required_connector_tags=[],
        )
        session.add(app)
        await session.commit()
        await session.refresh(app)
        app_id = app.id

    with patch(
        'src.capabilities.reconciliation.orchestrator.reconcile_accounts',
        side_effect=RuntimeError('boom'),
    ):
        async with session_factory() as session:
            with pytest.raises(RuntimeError, match='boom'):
                await reconcile_application(session, app_id, connector, log_service=log)
            await session.commit()

    assert len(captured) == 5
    root, enq_a, enq_r, enq_p, failed_ev = captured
    assert root.event_type == 'reconciliation.operation_started'
    assert failed_ev.event_type == 'reconciliation.failed'
    assert failed_ev.correlation_id == root.correlation_id
    assert failed_ev.causation_id == enq_p.event_id
    for e in (enq_a, enq_r, enq_p):
        assert e.correlation_id == root.correlation_id
    assert failed_ev.initiator_id == 'reconciliation'
    assert failed_ev.actor_id == 'reconciliation'
    assert failed_ev.target_id == str(app_id)


def test_simulated_reconciliation_connector_logs_preserve_initiator_and_actor_handoff() -> None:
    """After enqueue, connector received/completed mirror handler semantics (kernel-side model)."""
    root = new_root_log_event(
        event_type='reconciliation.operation_started',
        level=LogLevel.INFO,
        message='s',
        component='reconciliation',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='reconciliation',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='reconciliation',
        target_type=LogParticipantKind.APPLICATION,
        target_id='app-uuid',
        correlation_id='recon-corr-1',
        payload={},
    )
    enq = new_downstream_log_event(
        root,
        event_type='connector.command.enqueued',
        level=LogLevel.INFO,
        message='e',
        component='connector_client',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='reconciliation',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='reconciliation',
        target_type=LogParticipantKind.CONNECTOR,
        target_id='conn-z',
        payload={'operation': 'list_accounts'},
    )
    received = new_downstream_log_event_from_parent_id(
        parent_event_id=enq.event_id,
        correlation_id=enq.correlation_id,
        event_type='connector.command.received',
        level=LogLevel.INFO,
        message='r',
        component='connector',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='reconciliation',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn-z',
        target_type=LogParticipantKind.APPLICATION,
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
        initiator_id='reconciliation',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn-z',
        target_type=LogParticipantKind.APPLICATION,
        target_id='app-uuid',
        payload={},
    )

    assert completed.correlation_id == root.correlation_id
    assert completed.causation_id == received.event_id
    assert completed.initiator_id == 'reconciliation'
    assert completed.actor_id == 'conn-z'
    assert completed.target_id == 'app-uuid'

    for ev in (received, completed):
        parsed = parse_connector_log_payload(ev.model_dump(mode='json'))
        assert parsed is not None
        assert parsed.correlation_id == root.correlation_id
