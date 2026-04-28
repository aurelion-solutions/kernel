# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Ingest service for connector results.

Persists each accepted POST to ``staging_connector_results`` only for inline/lake_ref.
For result_type='artifacts_bulk', dispatches to AccessArtifactService.upsert_batch,
records a lake_batches row, and emits one 'inventory.access_artifacts.batch_ingested' event.
"""

from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.ingest.models import StagingConnectorResult
from src.capabilities.ingest.schemas import ArtifactsBulkPayload, ConnectorResultIngestRequest, LakeRefLocation
from src.inventory.access_artifacts.service import (
    AccessArtifactBatchItem,
    AccessArtifactLakeWriteError,
    AccessArtifactService,
)
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.applications.repository import get_application_by_id
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, NoOpEventService, noop_event_service
from src.platform.logs.schemas import LogLevel, LogParticipantKind
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_capability_trace_fields, noop_log_service

_COMPONENT = 'capabilities.ingest'


class ApplicationNotFoundError(Exception):
    """Application not found during validation."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application {application_id} not found')


def _parse_uuid(value: str) -> uuid.UUID:
    """Parse string to UUID, raising ValueError on invalid format."""
    return uuid.UUID(value)


def _location_to_payload(location: LakeRefLocation) -> dict[str, Any]:
    """Convert lake_ref location to payload dict for staging storage."""
    out: dict[str, Any] = {
        'provider': location.provider,
        'storage_key': location.storage_key,
    }
    if location.batch_id is not None:
        out['batch_id'] = location.batch_id
    return out


def _build_artifacts_batch_ingested_event(
    *,
    batch_id: uuid.UUID,
    ingested_count: int,
    tombstoned_count: int,
    snapshot_id: int | None,
    application_id: uuid.UUID | None,
    backend: str,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build the inventory.access_artifacts.batch_ingested EventEnvelope.

    Single builder for this event type — emitted only from capabilities/ingest/service.py.
    """
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_artifacts.batch_ingested',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
        causation_id=None,
        payload={
            'batch_id': str(batch_id),
            'ingested_count': ingested_count,
            'tombstoned_count': tombstoned_count,
            'snapshot_id': snapshot_id,
            'application_id': str(application_id) if application_id is not None else None,
            'backend': backend,
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id='capabilities.ingest',
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(batch_id),
    )


async def _dispatch_artifacts_bulk(
    session: AsyncSession,
    *,
    request: ConnectorResultIngestRequest,
    application_id: uuid.UUID,
    access_artifact_service: AccessArtifactService,
    lake_batch_service: LakeBatchService,
    event_service: EventService | NoOpEventService,
    log: LogService | NoOpLogService,
) -> None:
    """Dispatch artifacts_bulk result_type: upsert_batch + record_lake_write + emit event.

    Single emit site for inventory.access_artifacts.batch_ingested.
    """
    assert request.payload is not None  # guarded by schema validator

    try:
        bulk_payload = ArtifactsBulkPayload.model_validate(request.payload)
    except Exception as exc:
        raise ValueError(f'artifacts_bulk payload is invalid: {exc}') from exc

    log.emit_safe(
        level=LogLevel.INFO,
        message='capabilities.ingest.artifacts_bulk_started',
        component=_COMPONENT,
        payload=merge_emit_capability_trace_fields(
            {
                'ingest_batch_id': str(bulk_payload.ingest_batch_id),
                'item_count': len(bulk_payload.items),
                'application_id': str(bulk_payload.application_id),
            },
            capability_id='ingest',
            target_id=str(bulk_payload.application_id),
            target_type=LogParticipantKind.APPLICATION.value,
        ),
    )

    items = [
        AccessArtifactBatchItem(
            application_id=item.application_id,
            artifact_type=item.artifact_type,
            external_id=item.external_id,
            payload=item.payload,
            raw_name=item.raw_name,
            effect=item.effect,
            valid_from=item.valid_from,
            valid_until=item.valid_until,
            observed_at=item.observed_at,
        )
        for item in bulk_payload.items
    ]

    try:
        result = await access_artifact_service.upsert_batch(
            session,
            items,
            ingest_batch_id=bulk_payload.ingest_batch_id,
            correlation_id=request.code,
        )
    except AccessArtifactLakeWriteError:
        log.emit_safe(
            level=LogLevel.ERROR,
            message='capabilities.ingest.artifacts_bulk_failed',
            component=_COMPONENT,
            payload=merge_emit_capability_trace_fields(
                {
                    'ingest_batch_id': str(bulk_payload.ingest_batch_id),
                    'application_id': str(bulk_payload.application_id),
                },
                capability_id='ingest',
                target_id=str(bulk_payload.application_id),
                target_type=LogParticipantKind.APPLICATION.value,
            ),
        )
        raise

    # Record lake_batches row only when Iceberg snapshot was produced
    if result.snapshot_id is not None:
        await lake_batch_service.record_lake_write(
            session,
            dataset_type='access_artifacts',
            iceberg_namespace='raw',
            iceberg_table='access_artifacts',
            snapshot_id=result.snapshot_id,
            row_count=result.row_count,
            application_id=bulk_payload.application_id,
        )

    # Single emit site for this event type
    await event_service.emit(
        _build_artifacts_batch_ingested_event(
            batch_id=bulk_payload.ingest_batch_id,
            ingested_count=result.row_count,
            tombstoned_count=0,
            snapshot_id=result.snapshot_id,
            application_id=bulk_payload.application_id,
            backend=result.backend,
            correlation_id=request.code,
        )
    )

    log.emit_safe(
        level=LogLevel.INFO,
        message='capabilities.ingest.artifacts_bulk_completed',
        component=_COMPONENT,
        payload=merge_emit_capability_trace_fields(
            {
                'ingest_batch_id': str(bulk_payload.ingest_batch_id),
                'row_count': result.row_count,
                'snapshot_id': result.snapshot_id,
                'backend': result.backend,
                'application_id': str(bulk_payload.application_id),
            },
            capability_id='ingest',
            target_id=str(bulk_payload.application_id),
            target_type=LogParticipantKind.APPLICATION.value,
        ),
    )


async def ingest_connector_result(
    session: AsyncSession,
    request: ConnectorResultIngestRequest,
    validate_application: bool = True,
    log_service: LogService | None = None,
    access_artifact_service: AccessArtifactService | None = None,
    lake_batch_service: LakeBatchService | None = None,
    event_service: EventService | None = None,
) -> None:
    """Ingest connector result.

    - 'inline' / 'lake_ref': persist into staging_connector_results.
    - 'artifacts_bulk': dispatch to AccessArtifactService.upsert_batch,
      record lake_batches, emit inventory.access_artifacts.batch_ingested event.
    """
    log = log_service if log_service is not None else noop_log_service
    ev = event_service if event_service is not None else noop_event_service
    task_id = _parse_uuid(request.task_id)
    application_id = _parse_uuid(request.application_id)
    result_id = _parse_uuid(request.result_id)

    if validate_application:
        app = await get_application_by_id(session, application_id)
        if app is None:
            log.emit_safe(
                level=LogLevel.ERROR,
                message=f'Application {application_id} not found',
                component='ingest',
                payload=merge_emit_capability_trace_fields(
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

    if request.result_type == 'artifacts_bulk':
        _aa_service = access_artifact_service if access_artifact_service is not None else AccessArtifactService()
        _lb_service = lake_batch_service if lake_batch_service is not None else _make_noop_lake_batch_service()
        await _dispatch_artifacts_bulk(
            session,
            request=request,
            application_id=application_id,
            access_artifact_service=_aa_service,
            lake_batch_service=_lb_service,
            event_service=ev,
            log=log,
        )
        return

    payload_to_store: dict[str, Any] | None
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
        level=LogLevel.INFO,
        message='Connector result ingested',
        component='ingest',
        payload=merge_emit_capability_trace_fields(
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


def _make_noop_lake_batch_service() -> LakeBatchService:
    """Create a no-op LakeBatchService for fallback when DI is absent."""
    from src.platform.storage.factory import DataLakeStorageFactory

    return LakeBatchService(storage_factory=DataLakeStorageFactory())


async def _insert_connector_result(
    session: AsyncSession,
    task_id: uuid.UUID,
    application_id: uuid.UUID,
    operation: str,
    status: str,
    result_id: uuid.UUID,
    payload: dict[str, Any] | None,
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
