# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation orchestrator: coordinates full reconciliation for one application."""

from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.reconciler_account import reconcile_accounts
from src.capabilities.reconciliation.reconciler_privilege import reconcile_privileges
from src.capabilities.reconciliation.reconciler_role import reconcile_roles
from src.capabilities.reconciliation.schemas import ReconciliationResult
from src.inventory.accounts.schemas import AccountDTO
from src.inventory.privileges.schemas import PrivilegeDTO
from src.inventory.roles.schemas import RoleDTO
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.applications.models import Application
from src.platform.applications.repository import get_application_by_id
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.exceptions import ConnectorInstanceNotFoundError
from src.platform.connectors.result_expansion import expand_records_from_response
from src.platform.connectors.service import ConnectorInstanceService
from src.platform.logs.schemas import (
    LogEvent,
    LogLevel,
    LogParticipantKind,
    new_downstream_log_event,
    new_root_log_event,
)
from src.platform.logs.service import LogService, merge_emit_capability_trace_fields, noop_log_service


def _connector_payload_for_application(app: Application) -> dict[str, Any]:
    return {'config': app.config}


async def _fetch_connector_list_dataset(
    connector: ConnectorClient,
    instance_id: str,
    app: Application,
    operation: str,
    list_key: str,
    log: LogService,
    trace_parent: LogEvent,
    reconciliation_root: LogEvent,
) -> tuple[dict[str, Any], LogEvent]:
    """Fetch one list dataset via connector RPC with LogEvent v2 trace propagation.

    Returns ``(expanded_payload, enqueued_event)`` for chaining the next enqueue.
    """
    app_id_str = str(app.id)
    enqueued = new_downstream_log_event(
        trace_parent,
        event_type='connector.command.enqueued',
        level=LogLevel.INFO,
        message='Connector command enqueued',
        component='connector_client',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='reconciliation',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='reconciliation',
        target_type=LogParticipantKind.CONNECTOR,
        target_id=instance_id,
        payload={
            'application_id': app_id_str,
            'instance_id': instance_id,
            'operation': operation,
        },
    )
    log.emit_event_safe(enqueued)

    raw = await connector.invoke(
        instance_id,
        operation,
        _connector_payload_for_application(app),
        result_storage_requested=True,
        correlation_id=enqueued.correlation_id,
        trace_parent_event_id=enqueued.event_id,
        trace_initiator_type=reconciliation_root.initiator_type.value,
        trace_initiator_id=reconciliation_root.initiator_id,
        trace_target_type=reconciliation_root.target_type.value,
        trace_target_id=reconciliation_root.target_id,
    )
    expanded = expand_records_from_response(
        raw,
        list_key=list_key,
        lake_factory=connector.lake_factory,
    )
    return expanded, enqueued


def _validate_accounts_payload(payload: dict) -> list[AccountDTO]:
    """Validate raw payload into AccountDTO list. Expects {'accounts': [...]}."""
    raw = payload.get('accounts') or []
    return [AccountDTO.model_validate(item) for item in raw]


def _validate_roles_payload(payload: dict) -> list[RoleDTO]:
    """Validate raw payload into RoleDTO list. Expects {'roles': [...]}."""
    raw = payload.get('roles') or []
    return [RoleDTO.model_validate(item) for item in raw]


def _validate_privileges_payload(payload: dict) -> list[PrivilegeDTO]:
    """Validate raw payload into PrivilegeDTO list. Expects {'privileges': [...]}."""
    raw = payload.get('privileges') or []
    return [PrivilegeDTO.model_validate(item) for item in raw]


async def begin_reconciliation_trace(
    session: AsyncSession,
    application_id: uuid.UUID,
    log_service: LogService | None = None,
) -> tuple[Application, str, LogEvent]:
    """Validate app + connector binding, emit ``reconciliation.operation_started``, return trace root.

    Used by HTTP to return ``correlation_id`` before long-running work.
    """
    log = log_service if log_service is not None else noop_log_service
    app = await get_application_by_id(session, application_id)
    if app is None:
        log.emit_safe(
            'reconciliation.failed',
            LogLevel.ERROR,
            f'Application {application_id} not found',
            'reconciliation',
            merge_emit_capability_trace_fields(
                {
                    'application_id': str(application_id),
                    'reason': 'application_not_found',
                },
                capability_id='reconciliation',
                target_id=str(application_id),
                target_type=LogParticipantKind.APPLICATION.value,
            ),
        )
        raise ApplicationNotFoundError(f'Application {application_id} not found')

    instance_service = ConnectorInstanceService()
    instance_id = await instance_service.require_instance_id_for_application(session, app)

    target_scope = str(application_id)
    reconciliation_root = new_root_log_event(
        event_type='reconciliation.operation_started',
        level=LogLevel.INFO,
        message='Reconciliation operation started',
        component='reconciliation',
        initiator_type=LogParticipantKind.CAPABILITY,
        initiator_id='reconciliation',
        actor_type=LogParticipantKind.CAPABILITY,
        actor_id='reconciliation',
        target_type=LogParticipantKind.APPLICATION,
        target_id=target_scope,
        payload={'application_id': target_scope},
    )
    log.emit_event_safe(reconciliation_root)
    return app, instance_id, reconciliation_root


async def execute_reconciliation_continue(
    session: AsyncSession,
    application_id: uuid.UUID,
    instance_id: str,
    connector: ConnectorClient,
    reconciliation_root: LogEvent,
    log_service: LogService | None = None,
    *,
    app: Application | None = None,
) -> ReconciliationResult:
    """Run connector fetches, DTO validation, domain reconcilers, and terminal log events."""
    log = log_service if log_service is not None else noop_log_service
    trace_parent: LogEvent | None = reconciliation_root
    try:
        if app is None:
            app = await get_application_by_id(session, application_id)
        if app is None:
            failed = new_downstream_log_event(
                reconciliation_root,
                event_type='reconciliation.failed',
                level=LogLevel.ERROR,
                message='Application no longer exists',
                component='reconciliation',
                initiator_type=LogParticipantKind.CAPABILITY,
                initiator_id='reconciliation',
                actor_type=LogParticipantKind.CAPABILITY,
                actor_id='reconciliation',
                target_type=LogParticipantKind.APPLICATION,
                target_id=str(application_id),
                payload={
                    'application_id': str(application_id),
                    'reason': 'application_not_found',
                },
            )
            log.emit_event_safe(failed)
            raise ApplicationNotFoundError(
                f'Application {application_id} not found during reconciliation',
            )

        accounts_payload, trace_parent = await _fetch_connector_list_dataset(
            connector,
            instance_id,
            app,
            'list_accounts',
            'accounts',
            log,
            trace_parent,
            reconciliation_root,
        )
        accounts_dtos = _validate_accounts_payload(accounts_payload)

        roles_payload, trace_parent = await _fetch_connector_list_dataset(
            connector,
            instance_id,
            app,
            'list_roles',
            'roles',
            log,
            trace_parent,
            reconciliation_root,
        )
        roles_dtos = _validate_roles_payload(roles_payload)

        privileges_payload, trace_parent = await _fetch_connector_list_dataset(
            connector,
            instance_id,
            app,
            'list_privileges',
            'privileges',
            log,
            trace_parent,
            reconciliation_root,
        )
        privileges_dtos = _validate_privileges_payload(privileges_payload)

        accounts_result = await reconcile_accounts(session, application_id, accounts_dtos)
        roles_result = await reconcile_roles(session, application_id, roles_dtos)
        privileges_result = await reconcile_privileges(session, application_id, privileges_dtos)

        result = ReconciliationResult(
            application_id=str(application_id),
            accounts=accounts_result,
            roles=roles_result,
            privileges=privileges_result,
        )
        completed = new_downstream_log_event(
            trace_parent,
            event_type='reconciliation.completed',
            level=LogLevel.INFO,
            message='Reconciliation completed',
            component='reconciliation',
            initiator_type=LogParticipantKind.CAPABILITY,
            initiator_id='reconciliation',
            actor_type=LogParticipantKind.CAPABILITY,
            actor_id='reconciliation',
            target_type=LogParticipantKind.APPLICATION,
            target_id=str(application_id),
            payload={
                'application_id': str(application_id),
                'accounts_created': accounts_result.created,
                'accounts_updated': accounts_result.updated,
                'roles_created': roles_result.created,
                'roles_updated': roles_result.updated,
                'privileges_created': privileges_result.created,
                'privileges_updated': privileges_result.updated,
            },
        )
        log.emit_event_safe(completed)
        return result
    except ApplicationNotFoundError:
        raise
    except ConnectorInstanceNotFoundError:
        raise
    except Exception as e:
        if trace_parent is not None:
            failed = new_downstream_log_event(
                trace_parent,
                event_type='reconciliation.failed',
                level=LogLevel.ERROR,
                message=f'Reconciliation failed: {e!s}',
                component='reconciliation',
                initiator_type=LogParticipantKind.CAPABILITY,
                initiator_id='reconciliation',
                actor_type=LogParticipantKind.CAPABILITY,
                actor_id='reconciliation',
                target_type=LogParticipantKind.APPLICATION,
                target_id=str(application_id),
                payload={
                    'application_id': str(application_id),
                    'reason': type(e).__name__,
                },
            )
            log.emit_event_safe(failed)
        else:
            log.emit_safe(
                'reconciliation.failed',
                LogLevel.ERROR,
                f'Reconciliation failed: {e!s}',
                'reconciliation',
                merge_emit_capability_trace_fields(
                    {
                        'application_id': str(application_id),
                        'reason': type(e).__name__,
                    },
                    capability_id='reconciliation',
                    target_id=str(application_id),
                    target_type=LogParticipantKind.APPLICATION.value,
                ),
            )
        raise


async def reconcile_application(
    session: AsyncSession,
    application_id: uuid.UUID,
    connector: ConnectorClient,
    log_service: LogService | None = None,
) -> ReconciliationResult:
    """Orchestrate full reconciliation for one application (single session, blocking)."""
    app, instance_id, root = await begin_reconciliation_trace(
        session,
        application_id,
        log_service,
    )
    return await execute_reconciliation_continue(
        session,
        application_id,
        instance_id,
        connector,
        root,
        log_service,
        app=app,
    )
