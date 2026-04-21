# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RabbitMQEventSink contract tests — publisher mocked (7 tests)."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from src.platform.events.providers.mq import RabbitMQEventSink
from src.platform.events.schemas import EventEnvelope


def _make_envelope(event_type: str = 'inventory.access_fact.created') -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        correlation_id=str(uuid.uuid4()),
    )


def _make_mock_publisher() -> MagicMock:
    """Return a mock AsyncRabbitMQPublisher with an awaitable publish method."""
    publisher = MagicMock()
    publisher.publish = AsyncMock()
    return publisher


# ---------------------------------------------------------------------------
# 1. Default exchange is aurelion.events
# ---------------------------------------------------------------------------


async def test_emit_calls_publish_with_default_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('AURELION_EVENTS_EXCHANGE', raising=False)
    publisher = _make_mock_publisher()

    await RabbitMQEventSink(publisher).emit(_make_envelope())

    _kwargs: dict[str, Any] = publisher.publish.call_args.kwargs
    assert _kwargs['exchange'] == 'aurelion.events'


# ---------------------------------------------------------------------------
# 2. Exchange override via env var
# ---------------------------------------------------------------------------


async def test_emit_honours_events_exchange_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AURELION_EVENTS_EXCHANGE', 'test.events')
    publisher = _make_mock_publisher()

    await RabbitMQEventSink(publisher).emit(_make_envelope())

    _kwargs: dict[str, Any] = publisher.publish.call_args.kwargs
    assert _kwargs['exchange'] == 'test.events'


# ---------------------------------------------------------------------------
# 3. Exchange type is 'topic'
# ---------------------------------------------------------------------------


async def test_emit_uses_topic_exchange_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AURELION_EVENTS_EXCHANGE', raising=False)
    publisher = _make_mock_publisher()

    await RabbitMQEventSink(publisher).emit(_make_envelope())

    _kwargs: dict[str, Any] = publisher.publish.call_args.kwargs
    assert _kwargs['exchange_type'] == 'topic'


# ---------------------------------------------------------------------------
# 4. Routing key equals event_type byte-for-byte
# ---------------------------------------------------------------------------


async def test_emit_routing_key_equals_event_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AURELION_EVENTS_EXCHANGE', raising=False)
    publisher = _make_mock_publisher()
    event_type = 'inventory.access_fact.created'

    await RabbitMQEventSink(publisher).emit(_make_envelope(event_type=event_type))

    _kwargs: dict[str, Any] = publisher.publish.call_args.kwargs
    assert _kwargs['routing_key'] == event_type


# ---------------------------------------------------------------------------
# 5. Body is JSON-encoded model_dump — UUIDs → str, datetime → ISO
# ---------------------------------------------------------------------------


async def test_emit_serialises_via_model_dump_json_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.delenv('AURELION_EVENTS_EXCHANGE', raising=False)
    publisher = _make_mock_publisher()
    envelope = _make_envelope()

    await RabbitMQEventSink(publisher).emit(envelope)

    _kwargs: dict[str, Any] = publisher.publish.call_args.kwargs
    body_dict = json.loads(_kwargs['body'].decode('utf-8'))
    assert body_dict == envelope.model_dump(mode='json')

    # UUIDs must be strings in the serialised dict
    assert isinstance(body_dict['event_id'], str)
    assert isinstance(body_dict['occurred_at'], str)


# ---------------------------------------------------------------------------
# 6. Publish failure propagates (no swallowing)
# ---------------------------------------------------------------------------


async def test_emit_reraises_on_publish_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AURELION_EVENTS_EXCHANGE', raising=False)
    publisher = MagicMock()
    publisher.publish = AsyncMock(side_effect=RuntimeError('connection refused'))

    with pytest.raises(RuntimeError, match='connection refused'):
        await RabbitMQEventSink(publisher).emit(_make_envelope())


# ---------------------------------------------------------------------------
# 7. Env credentials NOT forwarded (publisher owns connection)
# ---------------------------------------------------------------------------


async def test_emit_does_not_pass_host_or_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publisher owns connection; the sink does NOT forward host/port/credentials."""
    monkeypatch.setenv('AURELION_RABBITMQ_HOST', 'mq.example.com')
    monkeypatch.setenv('AURELION_RABBITMQ_PORT', '5673')
    monkeypatch.setenv('AURELION_RABBITMQ_USERNAME', 'alice')
    monkeypatch.setenv('AURELION_RABBITMQ_PASSWORD', 's3cr3t')
    publisher = _make_mock_publisher()

    await RabbitMQEventSink(publisher).emit(_make_envelope())

    # publish() must NOT receive host/port/username/password — publisher owns them
    _kwargs: dict[str, Any] = publisher.publish.call_args.kwargs
    assert 'host' not in _kwargs
    assert 'username' not in _kwargs
