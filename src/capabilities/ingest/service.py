# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Ingest service for connector results.

Persists each accepted POST to ``staging_connector_results`` only. This is intentional
staging (task/result correlation, optional downstream use), not a replacement for
reconciliation-driven materialization into accounts/roles/privileges.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.ingest.models import StagingConnectorResult
from src.capabilities.ingest.schemas import ConnectorResultIngestRequest, LakeRefLocation
from src.platform.applications.repository import get_application_by_id
from src.platform.logs.schemas import LogLevel, LogParticipantKind
from src.platform.logs.service import LogService, merge_emit_capability_trace_fields, noop_log_service


class ApplicationNotFoundError(Exception):
    """Application not found during validation."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application {application_id} not found')


def _parse_uuid(value: str) -> uuid.UUID:
    """Parse string to UUID, raising ValueError on invalid format."""
    return uuid.UUID(value)


def _location_to_payload(location: LakeRefLocation) -> dict:
    """Convert lake_ref location to payload dict for staging storage."""
    out: dict = {
        'provider': location.provider,
        'storage_key': location.storage_key,
    }
    if location.batch_id is not None:
        out['batch_id'] = location.batch_id
    return out


async def ingest_connector_result(
    session: AsyncSession,
    request: ConnectorResultIngestRequest,
    validate_application: bool = True,
    log_service: LogService | None = None,
) -> None:
    """
    Ingest connector result into staging.

    Inserts one row into staging_connector_results. Inline stores payload;
    lake_ref stores location as payload dict.
    """
    log = log_service if log_service is not None else noop_log_service
    task_id = _parse_uuid(request.task_id)
    application_id = _parse_uuid(request.application_id)
    result_id = _parse_uuid(request.result_id)

    if validate_application:
        app = await get_application_by_id(session, application_id)
        if app is None:
            log.emit_safe(
                'ingest.application.not_found',
                LogLevel.ERROR,
                f'Application {application_id} not found',
                'ingest',
                merge_emit_capability_trace_fields(
                    {
                        'application_id': str(application_id),
                        'task_id': request.task_id,
                        'result_id': request.result_id,
                    },
                    capability_id='ingest',
                    target_id=str(application_id),
                    target_type=LogParticipantKind.APPLICATION.value,
                ),
            )
            raise ApplicationNotFoundError(application_id)

    payload_to_store: dict | None
    if request.result_type == 'lake_ref' and request.location is not None:
        payload_to_store = _location_to_payload(request.location)
    else:
        payload_to_store = request.payload

    await _insert_connector_result(
        session,
        task_id=task_id,
        application_id=application_id,
        operation=request.operation,
        status=request.status,
        result_id=result_id,
        payload=payload_to_store,
    )
    log.emit_safe(
        'ingest.result.received',
        LogLevel.INFO,
        'Connector result ingested',
        'ingest',
        merge_emit_capability_trace_fields(
            {
                'task_id': str(task_id),
                'application_id': str(application_id),
                'result_id': str(result_id),
                'operation': request.operation,
                'status': request.status,
                'result_type': request.result_type,
            },
            capability_id='ingest',
            target_id=str(application_id),
            target_type=LogParticipantKind.APPLICATION.value,
        ),
    )


async def _insert_connector_result(
    session: AsyncSession,
    task_id: uuid.UUID,
    application_id: uuid.UUID,
    operation: str,
    status: str,
    result_id: uuid.UUID,
    payload: dict | None,
) -> None:
    """Insert generic connector result row into staging table."""
    row = StagingConnectorResult(
        task_id=task_id,
        application_id=application_id,
        operation=operation,
        status=status,
        result_id=result_id,
        payload=payload,
    )
    session.add(row)
