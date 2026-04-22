# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EventSinkFactory contract tests (6 tests)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from src.platform.events import event_sink_factory
from src.platform.events.factory import EventSinkFactory, UnsupportedProviderError
from src.platform.events.interface import EventSink
from src.platform.events.providers.mq import RabbitMQEventSink
from src.platform.events.schemas import EventEnvelope


def _make_envelope() -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_fact.created',
        occurred_at=datetime.now(UTC),
        correlation_id=str(uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# 1. Default registrations (mq removed — wired in lifespan, not at import time)
# ---------------------------------------------------------------------------


def test_defaults_registered() -> None:
    factory = EventSinkFactory()
    assert factory.list_names() == ['noop']


# ---------------------------------------------------------------------------
# 2. noop sink implements EventSink and emits without raising
# ---------------------------------------------------------------------------


async def test_get_noop_returns_event_sink() -> None:
    factory = EventSinkFactory()
    sink = factory.get('noop')
    assert isinstance(sink, EventSink)
    result = await sink.emit(_make_envelope())
    assert result is None


# ---------------------------------------------------------------------------
# 3. mq sink is RabbitMQEventSink when registered; constructor is lazy (no connect on get)
# ---------------------------------------------------------------------------


def test_get_mq_returns_rabbitmq_sink_without_network() -> None:
    """Registering and calling factory.get('mq') must not open a network connection.

    RabbitMQEventSink.__init__ takes a publisher stub — no network call happens.
    """
    publisher_stub = MagicMock()
    publisher_stub.publish = AsyncMock()

    factory = EventSinkFactory()
    factory.register('mq', lambda: RabbitMQEventSink(publisher_stub, exchange='aurelion.events'))
    sink = factory.get('mq')
    assert isinstance(sink, RabbitMQEventSink)


# ---------------------------------------------------------------------------
# 4. Unknown provider raises UnsupportedProviderError
# ---------------------------------------------------------------------------


def test_get_unknown_raises() -> None:
    factory = EventSinkFactory()
    with pytest.raises(UnsupportedProviderError, match='kafka'):
        factory.get('kafka')


# ---------------------------------------------------------------------------
# 5. Custom provider can be registered and retrieved
# ---------------------------------------------------------------------------


async def test_register_custom_provider() -> None:
    factory = EventSinkFactory()
    captured: list[EventEnvelope] = []

    class _CaptureSink:
        async def emit(self, event: EventEnvelope) -> None:
            captured.append(event)

    factory.register('capture', _CaptureSink)
    sink = factory.get('capture')
    env = _make_envelope()
    await sink.emit(env)
    assert captured == [env]


# ---------------------------------------------------------------------------
# 6. Module-level singleton is an EventSinkFactory
# ---------------------------------------------------------------------------


def test_singleton_is_event_sink_factory_instance() -> None:
    assert isinstance(event_sink_factory, EventSinkFactory)
