# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.provisioning.schemas import AccountCreateRequest
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.applications.repository import get_application_by_id
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.service import ConnectorInstanceService
from src.platform.logs.schemas import LogLevel, LogParticipantKind, new_downstream_log_event, new_root_log_event
from src.platform.logs.service import LogService, merge_emit_capability_trace_fields, noop_log_service


async def create_account(
    session: AsyncSession,
    application_id: uuid.UUID,
    request: AccountCreateRequest,
    connector: ConnectorClient,
    log_service: LogService | None = None,
) -> dict:
    log = log_service if log_service is not None else noop_log_service
    app = await get_application_by_id(session, application_id)
    if app is None:
        # NOTE: kwarg-shape refactor (Step 23 Phase 10) — NOT a migration to aurelion.events bus.
        log.emit_safe(
            level=LogLevel.WARNING,
            message=f'Application {application_id} not found',
            component='applications',
            payload=merge_emit_capability_trace_fields(
                {'application_id': str(application_id)},
                capability_id='provisioning',
                target_id=str(application_id),
            ),
        )
        raise ApplicationNotFoundError(f'Application {application_id} not found')

    instance_service = ConnectorInstanceService()
    instance_id = await instance_service.require_instance_id_for_application(session, app)

    target_scope = str(application_id)
    started = new_root_log_event(
        event_type='provisioning.operation_started',
        level=LogLevel.INFO,
        message='Provisioning operation started',
        component='provisioning',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='provisioning',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='provisioning',
        target_type=LogParticipantKind.SYSTEM,
        target_id=target_scope,
        payload={
            'application_id': target_scope,
            'operation': 'create_account',
            'username': request.username,
        },
    )
    log.emit_event_safe(started)

    enqueued = new_downstream_log_event(
        started,
        event_type='connector.command.enqueued',
        level=LogLevel.INFO,
        message='Connector command enqueued',
        component='connector_client',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='provisioning',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='provisioning',
        target_type=LogParticipantKind.CONNECTOR,
        target_id=instance_id,
        payload={
            'application_id': target_scope,
            'instance_id': instance_id,
            'operation': 'create_account',
        },
    )
    log.emit_event_safe(enqueued)

    await connector.invoke(
        instance_id,
        'create_account',
        {
            'config': app.config,
            'username': request.username,
            'email': request.email,
        },
        result_storage_requested=False,
        correlation_id=enqueued.correlation_id,
        trace_parent_event_id=enqueued.event_id,
        trace_initiator_type=started.initiator_type.value,
        trace_initiator_id=started.initiator_id,
        trace_target_type=started.target_type.value,
        trace_target_id=started.target_id,
    )

    return {
        'username': request.username,
        'email': request.email,
        'status': 'accepted',
    }
