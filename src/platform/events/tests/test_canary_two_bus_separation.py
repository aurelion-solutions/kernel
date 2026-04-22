# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Canary tests: two-bus separation invariant (Phase 10 close).

These tests prove that the aurelion.events and aurelion.logs buses are structurally
separate — a domain event cannot leak onto the log bus via the LogService public API,
and the events bus is typologically isolated from LogEvent.

All tests are in-process: no broker, no real inventory services.
C4 guardian constraint: do NOT use real inventory services (AccessFactService etc.).
Use EventEnvelope directly for the positive path.
"""

from datetime import UTC, datetime
import inspect
import uuid

from pydantic import ValidationError
import pytest
from src.platform.events.schemas import EventEnvelope
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.service import LogService, NoOpLogService
from src.platform.logs.testing import CapturingLogSink


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def capturing_log_sink() -> CapturingLogSink:
    return CapturingLogSink()


@pytest.fixture
def log_service(capturing_log_sink: CapturingLogSink) -> LogService:
    factory = LogSinkFactory()
    factory.register('capture', lambda: capturing_log_sink)
    return LogService(sink=factory.get('capture'))


async def test_event_service_emit_goes_to_events_sink_not_log_sink(
    capturing_events: CapturingEventService,
    capturing_log_sink: CapturingLogSink,
) -> None:
    """Canary (Test 1): EventService.emit puts EventEnvelope in events sink, NOT in log sink.

    This is a pure structural test — no real inventory services, no broker.
    EventEnvelope is constructed directly.
    """
    service = EventService(sink=capturing_events)
    envelope = EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_fact.created',
        occurred_at=datetime.now(UTC),
        correlation_id=str(uuid.uuid4()),
        payload={'source': 'canary_test'},
    )
    await service.emit(envelope)

    # Events sink received exactly one envelope with the correct 3-segment routing key.
    assert len(capturing_events.emitted) == 1
    assert capturing_events.emitted[0].event_type == 'inventory.access_fact.created'

    # Log sink received zero records — no cross-bus leakage.
    assert capturing_log_sink.records == []


def test_log_service_emit_safe_has_no_event_type_parameter(
    log_service: LogService,
) -> None:
    """Canary (Test 2): LogService.emit_safe signature has no event_type parameter.

    If a future refactor re-introduces event_type on LogService, this test fails
    at introspection time, before any runtime emission happens.
    """
    sig_emit_safe = inspect.signature(log_service.emit_safe)
    assert 'event_type' not in sig_emit_safe.parameters, (
        'LogService.emit_safe must not accept event_type — domain routing belongs on aurelion.events'
    )

    sig_emit_log = inspect.signature(log_service.emit_log)
    assert 'event_type' not in sig_emit_log.parameters, (
        'LogService.emit_log must not accept event_type — domain routing belongs on aurelion.events'
    )


def test_event_envelope_rejects_two_segment_event_type() -> None:
    """Canary (Test 3): the 3-segment validator in EventEnvelope is the only gate.

    A 2-segment legacy-era string like 'inventory.access_fact' must be rejected.
    This proves that events bus grammar enforcement is structural, not social.
    """
    with pytest.raises(ValidationError):
        EventEnvelope(
            event_id=uuid.uuid4(),
            event_type='inventory.access_fact',  # 2-segment — legacy log-era shape
            occurred_at=datetime.now(UTC),
            correlation_id=str(uuid.uuid4()),
        )


def test_noop_log_service_emit_safe_has_no_event_type_parameter() -> None:
    """Canary (Test 4 / O2): NoOpLogService.emit_safe signature has no event_type parameter."""
    sig = inspect.signature(NoOpLogService().emit_safe)
    assert 'event_type' not in sig.parameters, 'NoOpLogService.emit_safe must not accept event_type'
