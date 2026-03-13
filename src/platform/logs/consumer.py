# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Consumer for MQ log events."""

from collections.abc import Callable
import json
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel
from pydantic import ValidationError
from src.core.mq.rabbitmq import declare_topic_exchange_fanout_queues
from src.platform.logs.schemas import LogEvent
from src.platform.logs.service import LogService

# Wire → model: enum fields are lowercase strings on the contract; timestamps ISO 8601.
_LOWER_ENUM_FIELDS = (
    'level',
    'initiator_type',
    'actor_type',
    'target_type',
)


def normalize_mq_log_event_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a decoded MQ JSON object for :class:`LogEvent` validation.

    Expected shape (tracing contract):

    - ``event_id``, ``event_type``, ``timestamp``, ``level``, ``message``, ``component``,
      ``correlation_id`` (string), ``payload``, ``initiator_type``, ``initiator_id``,
      ``actor_type``, ``actor_id``, ``target_type``, ``target_id``
    - ``causation_id``: optional; omit or ``null`` for trace roots

    Raises:
        ValueError: if ``payload`` is present but not a JSON object (or null).
    """
    normalized: dict[str, Any] = dict(raw)

    payload = normalized.get('payload', {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError('payload must be a JSON object or null')
    normalized['payload'] = payload

    for key in _LOWER_ENUM_FIELDS:
        val = normalized.get(key)
        if isinstance(val, str):
            normalized[key] = val.strip().lower()

    ts = normalized.get('timestamp')
    if isinstance(ts, str) and ts.endswith('Z') and '+' not in ts:
        normalized['timestamp'] = ts[:-1] + '+00:00'

    return normalized


def parse_connector_log_payload(raw: dict[str, Any]) -> LogEvent | None:
    """Parse and normalize MQ JSON body into :class:`LogEvent`. Returns ``None`` if invalid."""
    if not isinstance(raw, dict):
        return None
    try:
        normalized = normalize_mq_log_event_payload(raw)
        return LogEvent.model_validate(normalized)
    except (ValidationError, ValueError, TypeError):
        return None


def run_rabbitmq_consumer(
    *,
    host: str,
    port: int,
    exchange: str,
    queue_name: str,
    binding_keys: list[str],
    log_service: LogService,
    username: str | None = None,
    password: str | None = None,
    on_parse_error: Callable[[dict[str, Any], str], None] | None = None,
    companion_queues: tuple[str, ...] = (),
) -> None:
    """Consume connector log events from RabbitMQ.

    ``companion_queues`` are declared and bound alongside ``queue_name`` so the broker fans out
    copies to every queue (same binding keys); only ``queue_name`` is consumed here.
    """
    user = username if username is not None else 'guest'
    passwd = password if password is not None else 'guest'
    params = pika.ConnectionParameters(
        host=host,
        port=port,
        credentials=pika.PlainCredentials(username=user, password=passwd),
    )
    connection = pika.BlockingConnection(params)
    channel: BlockingChannel = connection.channel()

    topology_queues: list[str] = []
    for q in (queue_name, *companion_queues):
        if q not in topology_queues:
            topology_queues.append(q)
    declare_topic_exchange_fanout_queues(
        channel,
        exchange=exchange,
        queue_names=topology_queues,
        binding_keys=binding_keys,
    )

    def _callback(_ch: BlockingChannel, method: Any, _props: Any, body: bytes) -> None:
        try:
            raw = json.loads(body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _ch.basic_ack(delivery_tag=method.delivery_tag)
            if on_parse_error is not None:
                try:
                    on_parse_error({'raw': str(body[:200])}, str(exc))
                except Exception:
                    pass
            return

        if not isinstance(raw, dict):
            _ch.basic_ack(delivery_tag=method.delivery_tag)
            if on_parse_error is not None:
                try:
                    on_parse_error({'raw': raw}, 'Payload is not a dict')
                except Exception:
                    pass
            return

        event = parse_connector_log_payload(raw)
        if event is None:
            _ch.basic_ack(delivery_tag=method.delivery_tag)
            if on_parse_error is not None:
                try:
                    on_parse_error(raw, 'Malformed connector log payload')
                except Exception:
                    pass
            return

        log_service.emit_event_safe(event)
        _ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=queue_name, on_message_callback=_callback)

    try:
        channel.start_consuming()
    finally:
        connection.close()
