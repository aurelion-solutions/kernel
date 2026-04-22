# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for TeeEventSink."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.platform.events.schemas import EventEnvelope
from src.platform.events.tee_sink import TeeEventSink


def _envelope() -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type='test.entity.created',
        occurred_at=datetime.now(UTC),
        correlation_id=str(uuid4()),
    )


class _RecordingSink:
    def __init__(self) -> None:
        self.received: list[EventEnvelope] = []

    async def emit(self, event: EventEnvelope) -> None:
        self.received.append(event)


class _ExplodingSink:
    async def emit(self, event: EventEnvelope) -> None:
        raise RuntimeError('tap exploded')


@pytest.mark.asyncio
async def test_primary_called_with_event() -> None:
    primary = _RecordingSink()
    tap = _RecordingSink()
    sink = TeeEventSink(primary, tap)
    env = _envelope()
    await sink.emit(env)
    assert primary.received == [env]
    assert tap.received == [env]


@pytest.mark.asyncio
async def test_tap_exception_does_not_propagate() -> None:
    primary = _RecordingSink()
    bad_tap = _ExplodingSink()
    sink = TeeEventSink(primary, bad_tap)
    env = _envelope()
    # Should not raise
    await sink.emit(env)
    # Primary was still called
    assert primary.received == [env]
