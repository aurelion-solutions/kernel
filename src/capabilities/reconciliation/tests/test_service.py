# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for ReconciliationService (mocked pipeline)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.capabilities.reconciliation.schemas import ReconciliationRunSummary
from src.capabilities.reconciliation.service import ReconciliationService
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService


def _make_summary(facts_revoked: int = 0, facts_errored: int = 0) -> ReconciliationRunSummary:
    return ReconciliationRunSummary(
        application_id=uuid.uuid4(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        artifacts_ingested=1,
        facts_created=0,
        facts_updated=0,
        facts_revoked=facts_revoked,
        artifacts_unhandled=0,
        facts_errored=facts_errored,
    )


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events) -> EventService:
    return EventService(sink=capturing_events)


@pytest.mark.asyncio
async def test_run_delegates_to_pipeline_and_emits_completed(
    event_service: EventService,
    capturing_events: CapturingEventService,
):
    """run() calls pipeline, emits exactly one reconciliation.run.completed event."""
    summary = _make_summary()

    session_mock = AsyncMock()
    # session.get(Application, ...) must return a truthy object to pass the existence check
    session_mock.get = AsyncMock(return_value=MagicMock())
    svc = ReconciliationService(session=session_mock, events=event_service)

    with patch(
        'src.capabilities.reconciliation.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        result = await svc.run(summary.application_id)

    assert result is summary
    completed = capturing_events.filter_by_type('reconciliation.run.completed')
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_run_raises_application_not_found_when_session_get_returns_none(
    event_service: EventService,
):
    """run() raises ApplicationNotFoundError when session.get(Application) returns None."""
    session_mock = AsyncMock()
    session_mock.get = AsyncMock(return_value=None)
    svc = ReconciliationService(session=session_mock, events=event_service)

    with pytest.raises(ApplicationNotFoundError):
        await svc.run(uuid.uuid4())


@pytest.mark.asyncio
async def test_run_calls_log_emit_safe_when_facts_errored(
    event_service: EventService,
    capturing_events: CapturingEventService,
):
    """run() calls logs.emit_safe(WARNING) when summary.facts_errored > 0."""
    summary = _make_summary(facts_errored=3)

    session_mock = AsyncMock()
    session_mock.get = AsyncMock(return_value=MagicMock())

    log_service_mock = MagicMock(spec=LogService)
    svc = ReconciliationService(session=session_mock, events=event_service, logs=log_service_mock)

    with patch(
        'src.capabilities.reconciliation.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        await svc.run(summary.application_id)

    log_service_mock.emit_safe.assert_called_once()
    call_kwargs = log_service_mock.emit_safe.call_args
    assert call_kwargs.kwargs.get('level') == LogLevel.WARNING or (
        len(call_kwargs.args) > 0 and call_kwargs.args[0] == LogLevel.WARNING
    )


@pytest.mark.asyncio
async def test_run_does_not_call_log_emit_safe_when_no_errors(
    event_service: EventService,
):
    """run() does NOT call logs.emit_safe when facts_errored == 0."""
    summary = _make_summary(facts_errored=0)

    session_mock = AsyncMock()
    session_mock.get = AsyncMock(return_value=MagicMock())

    log_service_mock = MagicMock(spec=LogService)
    svc = ReconciliationService(session=session_mock, events=event_service, logs=log_service_mock)

    with patch(
        'src.capabilities.reconciliation.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        await svc.run(summary.application_id)

    log_service_mock.emit_safe.assert_not_called()


@pytest.mark.asyncio
async def test_emit_completed_uses_correct_routing_key(
    event_service: EventService,
    capturing_events: CapturingEventService,
):
    """emit_completed produces event_type='reconciliation.run.completed' (3-segment)."""
    summary = _make_summary()
    session_mock = AsyncMock()
    svc = ReconciliationService(session=session_mock, events=event_service)
    await svc.emit_completed(summary, correlation_id=None)

    assert len(capturing_events.emitted) == 1
    assert capturing_events.emitted[0].event_type == 'reconciliation.run.completed'


@pytest.mark.asyncio
async def test_emit_completed_warning_on_bulk_revoke(
    event_service: EventService,
    capturing_events: CapturingEventService,
):
    """emit_completed sets level=WARNING when facts_revoked > 100."""
    summary = _make_summary(facts_revoked=101)
    session_mock = AsyncMock()
    svc = ReconciliationService(session=session_mock, events=event_service)
    await svc.emit_completed(summary, correlation_id=None)

    event = capturing_events.emitted[0]
    assert event.payload['level'] == 'WARNING'
