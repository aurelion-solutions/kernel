# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for InMemoryEventBuffer."""

from datetime import UTC, datetime
from uuid import uuid4

from src.platform.events.buffer import InMemoryEventBuffer
from src.platform.events.schemas import EventEnvelope


def _envelope(event_type: str = 'test.entity.created') -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        correlation_id=str(uuid4()),
    )


def test_empty_snapshot_returns_empty_list() -> None:
    buf = InMemoryEventBuffer()
    assert buf.snapshot(limit=10) == []


def test_snapshot_returns_newest_first() -> None:
    buf = InMemoryEventBuffer()
    e1 = _envelope('test.entity.created')
    e2 = _envelope('test.entity.updated')
    e3 = _envelope('test.entity.deleted')
    buf.append(e1)
    buf.append(e2)
    buf.append(e3)
    result = buf.snapshot(limit=10)
    assert result == [e3, e2, e1]


def test_bounded_by_maxlen() -> None:
    buf = InMemoryEventBuffer(maxlen=3)
    envelopes = [_envelope() for _ in range(5)]
    for e in envelopes:
        buf.append(e)
    result = buf.snapshot(limit=10)
    # Only the last 3 appended survive; newest first
    assert result == list(reversed(envelopes[-3:]))
