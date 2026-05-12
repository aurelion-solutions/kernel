# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for connector.result.received event emission (Phase 18 Step 10).

Covers the inline and lake_ref success paths in ingest_connector_result.
The artifacts_bulk branch is intentionally excluded from this event.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.core.context import correlation_id_var
from src.engines.ingest.schemas import ConnectorResultIngestRequest, LakeRefLocation
from src.engines.ingest.service import ApplicationNotFoundError, ingest_connector_result
from src.inventory.access_artifacts.service import AccessArtifactService, BatchUpsertResult
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.testing import CapturingEventService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inline_request(
    app_id: uuid.UUID,
    *,
    code: str | None = None,
) -> ConnectorResultIngestRequest:
    return ConnectorResultIngestRequest(
        task_id=str(uuid.uuid4()),
        application_id=str(app_id),
        operation='reconcile',
        status='completed',
        result_type='inline',
        result_id=str(uuid.uuid4()),
        code=code,
        payload={'data': 'value'},
    )


def _make_lake_ref_request(
    app_id: uuid.UUID,
    *,
    code: str | None = None,
) -> ConnectorResultIngestRequest:
    return ConnectorResultIngestRequest(
        task_id=str(uuid.uuid4()),
        application_id=str(app_id),
        operation='sync',
        status='completed',
        result_type='lake_ref',
        result_id=str(uuid.uuid4()),
        code=code,
        location=LakeRefLocation(provider='file', storage_key='dataset/path'),
    )


def _make_artifacts_bulk_request(
    app_id: uuid.UUID,
    ingest_batch_id: uuid.UUID,
) -> ConnectorResultIngestRequest:
    items = [
        {
            'application_id': str(app_id),
            'artifact_type': 'sap_role',
            'external_id': 'role-0',
            'payload': {'name': 'Role 0'},
        }
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


def _make_mock_aa_service() -> MagicMock:
    svc = MagicMock(spec=AccessArtifactService)
    svc.upsert_batch = AsyncMock(return_value=BatchUpsertResult(row_count=1, snapshot_id=42, backend='iceberg'))
    return svc


def _make_mock_lb_service() -> MagicMock:
    svc = MagicMock(spec=LakeBatchService)
    svc.record_lake_write = AsyncMock(return_value=MagicMock())
    return svc


async def _create_application(session_factory: Any) -> uuid.UUID:
    """Insert a real Application row and return its id."""
    from src.platform.applications.models import Application  # noqa: PLC0415

    async with session_factory() as session:
        app = Application(
            name=f'test-crr-{uuid.uuid4()}',
            code=f'crr-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        app_id = app.id
        await session.commit()
    return app_id


# ---------------------------------------------------------------------------
# Test 1: inline path emits connector.result.received
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inline_path_emits_connector_result_received(session_factory: Any) -> None:
    """inline result_type: exactly one connector.result.received event emitted."""
    app_id = await _create_application(session_factory)
    capturing_events = CapturingEventService()
    request = _make_inline_request(app_id)

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            request,
            validate_application=False,
            event_service=capturing_events,
        )
        await session.commit()

    events = capturing_events.filter_by_type('connector.result.received')
    assert len(events) == 1

    evt: EventEnvelope = events[0]
    assert evt.payload['result_id'] == request.result_id
    assert evt.payload['application_id'] == str(app_id)
    assert evt.payload['task_id'] == request.task_id
    assert 'now' in evt.payload, "payload must include 'now' for pipeline args_from_payload"
    assert evt.actor_kind == EventParticipantKind.COMPONENT
    assert evt.actor_id == 'engines.ingest'
    assert evt.target_kind == EventParticipantKind.SYSTEM
    assert evt.target_id == str(app_id)


# ---------------------------------------------------------------------------
# Test 2: lake_ref path emits connector.result.received
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lake_ref_path_emits_connector_result_received(session_factory: Any) -> None:
    """lake_ref result_type: exactly one connector.result.received event emitted."""
    app_id = await _create_application(session_factory)
    capturing_events = CapturingEventService()
    request = _make_lake_ref_request(app_id)

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            request,
            validate_application=False,
            event_service=capturing_events,
        )
        await session.commit()

    events = capturing_events.filter_by_type('connector.result.received')
    assert len(events) == 1

    evt: EventEnvelope = events[0]
    assert evt.payload['result_id'] == request.result_id
    assert evt.payload['application_id'] == str(app_id)
    assert evt.payload['task_id'] == request.task_id
    assert 'now' in evt.payload, "payload must include 'now' for pipeline args_from_payload"
    assert evt.actor_kind == EventParticipantKind.COMPONENT
    assert evt.actor_id == 'engines.ingest'
    assert evt.target_kind == EventParticipantKind.SYSTEM
    assert evt.target_id == str(app_id)


# ---------------------------------------------------------------------------
# Test 3: artifacts_bulk path does NOT emit connector.result.received
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_bulk_path_does_not_emit_connector_result_received(
    session_factory: Any,
) -> None:
    """artifacts_bulk path: connector.result.received must NOT be emitted."""
    app_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    capturing_events = CapturingEventService()
    request = _make_artifacts_bulk_request(app_id, batch_id)

    mock_aa = _make_mock_aa_service()
    mock_lb = _make_mock_lb_service()

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            request,
            validate_application=False,
            access_artifact_service=mock_aa,
            lake_batch_service=mock_lb,
            event_service=capturing_events,
        )

    assert capturing_events.filter_by_type('connector.result.received') == []


# ---------------------------------------------------------------------------
# Test 4: no emit when application validation fails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_emit_when_application_validation_fails(session_factory: Any) -> None:
    """validate_application=True + app not found: ApplicationNotFoundError raised, no event."""
    app_id = uuid.uuid4()
    capturing_events = CapturingEventService()
    request = _make_inline_request(app_id)

    async with session_factory() as session:
        with (
            patch(
                'src.engines.ingest.service.get_application_by_id',
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(ApplicationNotFoundError),
        ):
            await ingest_connector_result(
                session,
                request,
                validate_application=True,
                event_service=capturing_events,
            )

    assert capturing_events.filter_by_type('connector.result.received') == []


# ---------------------------------------------------------------------------
# Test 5: correlation_id passthrough
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correlation_id_passthrough(session_factory: Any) -> None:
    """request.code used as correlation_id; when None, a generated non-empty string is used."""
    # Sub-case A: explicit code -> correlation_id == code
    app_id_a = await _create_application(session_factory)
    capturing_a = CapturingEventService()
    request_a = _make_inline_request(app_id_a, code='corr-abc-123')

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            request_a,
            validate_application=False,
            event_service=capturing_a,
        )
        await session.commit()

    events_a = capturing_a.filter_by_type('connector.result.received')
    assert len(events_a) == 1
    assert events_a[0].correlation_id == 'corr-abc-123'

    # Sub-case B: code=None, no ContextVar set -> generated non-empty string
    app_id_b = await _create_application(session_factory)
    capturing_b = CapturingEventService()
    request_b = _make_inline_request(app_id_b, code=None)

    # Ensure ContextVar is unset
    token = correlation_id_var.set(None)
    try:
        async with session_factory() as session:
            await ingest_connector_result(
                session,
                request_b,
                validate_application=False,
                event_service=capturing_b,
            )
            await session.commit()
    finally:
        correlation_id_var.reset(token)

    events_b = capturing_b.filter_by_type('connector.result.received')
    assert len(events_b) == 1
    assert len(events_b[0].correlation_id) > 0


# ---------------------------------------------------------------------------
# Test 6: emitter includes 'now' as ISO-8601 datetime string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emitter_includes_now_field(session_factory: Any) -> None:
    """connector.result.received payload must include 'now' as an ISO-8601 datetime string.

    The application_sync pipeline declares args.now as required (format: date-time).
    If the emitter omits 'now', the first real MQ delivery fails JSON Schema validation.
    """
    from datetime import datetime  # noqa: PLC0415

    app_id = await _create_application(session_factory)
    capturing_events = CapturingEventService()
    request = _make_inline_request(app_id)

    async with session_factory() as session:
        await ingest_connector_result(
            session,
            request,
            validate_application=False,
            event_service=capturing_events,
        )
        await session.commit()

    events = capturing_events.filter_by_type('connector.result.received')
    assert len(events) == 1
    payload = events[0].payload
    assert 'now' in payload, "emitter must include 'now' in payload"
    # Must be parseable as ISO-8601 datetime
    parsed = datetime.fromisoformat(payload['now'])
    assert parsed.tzinfo is not None, "'now' must be timezone-aware"
