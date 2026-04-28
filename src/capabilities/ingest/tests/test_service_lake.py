# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ingest service — artifacts_bulk path (Phase 15 Step 6)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.capabilities.ingest.schemas import ConnectorResultIngestRequest
from src.capabilities.ingest.service import (
    ingest_connector_result,
)
from src.inventory.access_artifacts.service import (
    AccessArtifactLakeWriteError,
    AccessArtifactService,
    BatchUpsertResult,
)
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.events.schemas import EventEnvelope
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


def _make_artifacts_bulk_request(
    app_id: uuid.UUID,
    ingest_batch_id: uuid.UUID,
    n_items: int = 3,
) -> ConnectorResultIngestRequest:
    """Build a ConnectorResultIngestRequest with result_type='artifacts_bulk'."""
    items = [
        {
            'application_id': str(app_id),
            'artifact_type': 'sap_role',
            'external_id': f'role-{i}',
            'payload': {'name': f'Role {i}'},
        }
        for i in range(n_items)
    ]
    return ConnectorResultIngestRequest(
        task_id=str(uuid.uuid4()),
        application_id=str(app_id),
        operation='ingest_artifacts',
        status='completed',
        result_type='artifacts_bulk',
        result_id=str(uuid.uuid4()),
        payload={
            'ingest_batch_id': str(ingest_batch_id),
            'application_id': str(app_id),
            'items': items,
        },
    )


def _make_mock_session() -> MagicMock:
    return MagicMock()


def _make_mock_aa_service(
    row_count: int = 3,
    snapshot_id: int | None = 42,
    backend: str = 'iceberg',
) -> MagicMock:
    svc = MagicMock(spec=AccessArtifactService)
    svc.upsert_batch = AsyncMock(
        return_value=BatchUpsertResult(row_count=row_count, snapshot_id=snapshot_id, backend=backend)
    )
    return svc


def _make_mock_lb_service() -> MagicMock:
    svc = MagicMock(spec=LakeBatchService)
    svc.record_lake_write = AsyncMock(return_value=MagicMock())
    return svc


# ---------------------------------------------------------------------------
# Test 1: upsert_batch called with correct args
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_artifacts_bulk_calls_upsert_batch(session_factory: Any) -> None:
    """artifacts_bulk: AccessArtifactService.upsert_batch called once with 3 items."""
    log, _ = _make_log_service()
    capturing_events = CapturingEventService()

    app_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    request = _make_artifacts_bulk_request(app_id, batch_id, n_items=3)

    mock_aa = _make_mock_aa_service(row_count=3, snapshot_id=99, backend='iceberg')
    mock_lb = _make_mock_lb_service()

    async with session_factory() as session:
        with patch(
            'src.capabilities.ingest.service.get_application_by_id',
            new_callable=AsyncMock,
            return_value=MagicMock(id=app_id),
        ):
            await ingest_connector_result(
                session,
                request,
                validate_application=False,
                log_service=log,
                access_artifact_service=mock_aa,
                lake_batch_service=mock_lb,
                event_service=capturing_events,
            )

    mock_aa.upsert_batch.assert_called_once()
    call_args = mock_aa.upsert_batch.call_args
    items_passed = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get('items', [])
    assert len(items_passed) == 3
    assert call_args.kwargs.get('ingest_batch_id') == batch_id


# ---------------------------------------------------------------------------
# Test 2: batch event emitted with full payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_artifacts_bulk_emits_batch_event(session_factory: Any) -> None:
    """artifacts_bulk: one inventory.access_artifacts.batch_ingested event emitted."""
    log, _ = _make_log_service()
    capturing_events = CapturingEventService()

    app_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    request = _make_artifacts_bulk_request(app_id, batch_id, n_items=3)

    mock_aa = _make_mock_aa_service(row_count=3, snapshot_id=99, backend='iceberg')
    mock_lb = _make_mock_lb_service()

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            request,
            validate_application=False,
            log_service=log,
            access_artifact_service=mock_aa,
            lake_batch_service=mock_lb,
            event_service=capturing_events,
        )

    events = capturing_events.filter_by_type('inventory.access_artifacts.batch_ingested')
    assert len(events) == 1
    evt: EventEnvelope = events[0]
    assert evt.payload['batch_id'] == str(batch_id)
    assert evt.payload['ingested_count'] == 3
    assert evt.payload['tombstoned_count'] == 0
    assert evt.payload['snapshot_id'] == 99
    assert evt.payload['application_id'] == str(app_id)
    assert evt.payload['backend'] == 'iceberg'


# ---------------------------------------------------------------------------
# Test 3: pg backend — record_lake_write NOT called (no iceberg snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_artifacts_bulk_pg_backend_records_no_lake_batch(session_factory: Any) -> None:
    """artifacts_bulk with pg backend: LakeBatchService.record_lake_write not called."""
    log, _ = _make_log_service()
    capturing_events = CapturingEventService()

    app_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    request = _make_artifacts_bulk_request(app_id, batch_id)

    mock_aa = _make_mock_aa_service(row_count=3, snapshot_id=None, backend='pg')
    mock_lb = _make_mock_lb_service()

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            request,
            validate_application=False,
            log_service=log,
            access_artifact_service=mock_aa,
            lake_batch_service=mock_lb,
            event_service=capturing_events,
        )

    mock_lb.record_lake_write.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: inline and lake_ref paths unchanged (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_inline_and_lake_ref_paths_unchanged(session_factory: Any) -> None:
    """inline/lake_ref paths insert into staging_connector_results, do NOT call upsert_batch."""
    from sqlalchemy import select
    from src.capabilities.ingest.models import StagingConnectorResult
    from src.platform.applications.models import Application

    log, _ = _make_log_service()
    mock_aa = _make_mock_aa_service()
    mock_lb = _make_mock_lb_service()
    capturing_events = CapturingEventService()

    # Create a real application to satisfy FK constraint
    async with session_factory() as session:
        app = Application(
            name=f'test-ingest-inline-{uuid.uuid4()}',
            code=f'app-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        app_id = app.id
        await session.commit()

    inline_req = ConnectorResultIngestRequest(
        task_id=str(uuid.uuid4()),
        application_id=str(app_id),
        operation='reconcile',
        status='completed',
        result_type='inline',
        result_id=str(uuid.uuid4()),
        payload={'data': 'value'},
    )

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            inline_req,
            validate_application=False,
            log_service=log,
            access_artifact_service=mock_aa,
            lake_batch_service=mock_lb,
            event_service=capturing_events,
        )
        await session.commit()

    async with session_factory() as session:
        rows = (await session.execute(select(StagingConnectorResult))).scalars().all()

    assert len(rows) >= 1
    mock_aa.upsert_batch.assert_not_called()
    events = capturing_events.filter_by_type('inventory.access_artifacts.batch_ingested')
    assert len(events) == 0


# ---------------------------------------------------------------------------
# Test 5: invalid payload raises ValueError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_artifacts_bulk_invalid_payload_raises(session_factory: Any) -> None:
    """artifacts_bulk with missing 'items' in payload raises ValueError."""
    log, _ = _make_log_service()
    app_id = uuid.uuid4()

    bad_req = ConnectorResultIngestRequest(
        task_id=str(uuid.uuid4()),
        application_id=str(app_id),
        operation='ingest',
        status='completed',
        result_type='artifacts_bulk',
        result_id=str(uuid.uuid4()),
        payload={'ingest_batch_id': str(uuid.uuid4()), 'application_id': str(app_id)},
        # missing 'items'
    )

    mock_aa = _make_mock_aa_service()
    mock_lb = _make_mock_lb_service()
    capturing_events = CapturingEventService()

    async with session_factory() as session:
        with pytest.raises(ValueError, match='artifacts_bulk payload is invalid'):
            await ingest_connector_result(
                session,
                bad_req,
                validate_application=False,
                log_service=log,
                access_artifact_service=mock_aa,
                lake_batch_service=mock_lb,
                event_service=capturing_events,
            )


# ---------------------------------------------------------------------------
# Test 6: lake write error — no event emitted, ERROR logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_artifacts_bulk_propagates_lake_write_error(session_factory: Any) -> None:
    """artifacts_bulk: AccessArtifactLakeWriteError re-raised; no event; ERROR logged."""
    log, sink = _make_log_service()
    capturing_events = CapturingEventService()

    app_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    request = _make_artifacts_bulk_request(app_id, batch_id)

    cause_exc = Exception('iceberg down')
    mock_aa = MagicMock(spec=AccessArtifactService)
    mock_aa.upsert_batch = AsyncMock(
        side_effect=AccessArtifactLakeWriteError('Lake write failed [append]: iceberg down', cause=cause_exc)
    )
    mock_lb = _make_mock_lb_service()

    async with session_factory() as session:
        with pytest.raises(AccessArtifactLakeWriteError):
            await ingest_connector_result(
                session,
                request,
                validate_application=False,
                log_service=log,
                access_artifact_service=mock_aa,
                lake_batch_service=mock_lb,
                event_service=capturing_events,
            )

    # No batch event emitted
    events = capturing_events.filter_by_type('inventory.access_artifacts.batch_ingested')
    assert len(events) == 0

    # ERROR log emitted
    from src.platform.logs.schemas import LogLevel

    error_logs = [r for r in sink.records if r.level == LogLevel.ERROR]
    assert any('artifacts_bulk_failed' in r.message for r in error_logs)
