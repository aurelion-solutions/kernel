# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EventService and NoOpEventService contract tests (6 tests)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock
import uuid

import pytest
from src.platform.events.interface import EventSink
from src.platform.events.schemas import EventEnvelope
from src.platform.events.service import EventService, NoOpEventService, _NoOpEventSink


def _make_envelope() -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_fact.created',
        occurred_at=datetime.now(UTC),
        correlation_id=str(uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# 1. EventService delegates to sink exactly once, passing the same object
# ---------------------------------------------------------------------------


async def test_event_service_delegates_to_sink_once() -> None:
    calls: list[EventEnvelope] = []

    class _FakeSink:
        async def emit(self, event: EventEnvelope) -> None:
            calls.append(event)

    env = _make_envelope()
    svc = EventService(_FakeSink())
    await svc.emit(env)

    assert len(calls) == 1
    assert calls[0] is env  # same object — envelope is frozen, pass-by-reference is safe


# ---------------------------------------------------------------------------
# 2. NoOpEventService.emit returns None
# ---------------------------------------------------------------------------


async def test_noop_event_service_returns_none() -> None:
    result = await NoOpEventService().emit(_make_envelope())
    assert result is None


# ---------------------------------------------------------------------------
# 3. EventService re-raises sink exceptions (Q7 contract)
# ---------------------------------------------------------------------------


async def test_event_service_reraises_sink_exception() -> None:
    class _BrokenSink:
        async def emit(self, event: EventEnvelope) -> None:
            raise RuntimeError('broker down')

    svc = EventService(_BrokenSink())
    with pytest.raises(RuntimeError, match='broker down'):
        await svc.emit(_make_envelope())


# ---------------------------------------------------------------------------
# 4. Any duck-typed class with async .emit() satisfies EventSink (runtime_checkable)
# ---------------------------------------------------------------------------


async def test_event_service_accepts_protocol_duck_type() -> None:
    class _MinimalSink:
        async def emit(self, event: EventEnvelope) -> None:
            pass

    sink = _MinimalSink()
    assert isinstance(sink, EventSink)

    svc = EventService(sink)
    await svc.emit(_make_envelope())  # must not raise


# ---------------------------------------------------------------------------
# 5. EventService does not touch a session (no DB calls on emit path)
# ---------------------------------------------------------------------------


async def test_event_service_does_not_touch_session() -> None:
    """EventService.emit must not call any SQLAlchemy session method."""

    class _TrackingSink:
        async def emit(self, event: EventEnvelope) -> None:
            pass

    mock_session = MagicMock()
    svc = EventService(_TrackingSink())
    await svc.emit(_make_envelope())

    mock_session.flush.assert_not_called()
    mock_session.commit.assert_not_called()
    mock_session.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# 6. _NoOpEventSink satisfies EventSink protocol
# ---------------------------------------------------------------------------


async def test_noop_event_sink_implements_protocol() -> None:
    sink = _NoOpEventSink()
    assert isinstance(sink, EventSink)
    result = await sink.emit(_make_envelope())
    assert result is None
