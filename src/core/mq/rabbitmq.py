# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RabbitMQ messaging infrastructure."""

from collections.abc import Callable
import json
from typing import Any

import pika
from pika.adapters.blocking_connection import BlockingChannel


def declare_consumer_topology(
    channel: BlockingChannel,
    *,
    exchange: str,
    exchange_type: str,
    queue_name: str,
    binding_keys: list[str],
) -> None:
    """Declare durable exchange and queue, then bind the queue for each routing key."""
    channel.exchange_declare(
        exchange=exchange,
        exchange_type=exchange_type,
        durable=True,
    )
    channel.queue_declare(queue=queue_name, durable=True)
    for binding_key in binding_keys:
        channel.queue_bind(
            queue=queue_name,
            exchange=exchange,
            routing_key=binding_key,
        )


def declare_topic_exchange_fanout_queues(
    channel: BlockingChannel,
    *,
    exchange: str,
    queue_names: list[str],
    binding_keys: list[str],
) -> None:
    """Declare one durable topic exchange and multiple queues; bind each queue with every key.

    For the same routing key pattern on each queue, RabbitMQ delivers a copy per queue (fan-out).
    """
    channel.exchange_declare(
        exchange=exchange,
        exchange_type='topic',
        durable=True,
    )
    for q in queue_names:
        channel.queue_declare(queue=q, durable=True)
        for binding_key in binding_keys:
            channel.queue_bind(
                queue=q,
                exchange=exchange,
                routing_key=binding_key,
            )


def _default_params(
    host: str = 'localhost',
    port: int = 5672,
    username: str | None = None,
    password: str | None = None,
) -> pika.ConnectionParameters:
    user = username if username is not None else 'guest'
    passwd = password if password is not None else 'guest'
    credentials = pika.PlainCredentials(username=user, password=passwd)
    return pika.ConnectionParameters(host=host, port=port, credentials=credentials)


def run_rabbitmq_consumer(
    *,
    on_event: Callable[[dict[str, Any], str, Any], None],
    host: str = 'localhost',
    port: int = 5672,
    exchange: str,
    exchange_type: str,
    queue_name: str,
    binding_keys: list[str],
    username: str | None = None,
    password: str | None = None,
) -> None:
    """Consume JSON messages from an exchange-bound queue forever."""
    params = _default_params(host=host, port=port, username=username, password=password)
    connection = pika.BlockingConnection(params)
    channel: BlockingChannel = connection.channel()

    declare_consumer_topology(
        channel,
        exchange=exchange,
        exchange_type=exchange_type,
        queue_name=queue_name,
        binding_keys=binding_keys,
    )

    def _callback(_ch: BlockingChannel, method: Any, props: Any, body: bytes) -> None:
        try:
            raw = json.loads(body.decode('utf-8'))
            if not isinstance(raw, dict):
                raise ValueError('Payload is not a JSON object')
            on_event(raw, method.routing_key, props)
        except Exception:  # noqa: BLE001 # allowed-broad: task-loop guard
            _ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        _ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=queue_name, on_message_callback=_callback)

    try:
        channel.start_consuming()
    finally:
        connection.close()
