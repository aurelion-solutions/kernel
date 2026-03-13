# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RabbitMQ messaging infrastructure."""

from collections.abc import Callable
import json
import time
from typing import Any
import uuid

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


def publish_json_message(
    *,
    host: str = 'localhost',
    port: int = 5672,
    exchange: str = '',
    exchange_type: str = 'direct',
    routing_key: str = '',
    message: dict[str, Any],
    username: str | None = None,
    password: str | None = None,
    correlation_id: str | None = None,
    reply_to: str | None = None,
    durable: bool = True,
) -> None:
    """Publish one JSON message and close the connection."""
    params = _default_params(host=host, port=port, username=username, password=password)
    connection = pika.BlockingConnection(params)
    channel: BlockingChannel = connection.channel()

    if exchange:
        channel.exchange_declare(
            exchange=exchange,
            exchange_type=exchange_type,
            durable=durable,
        )

    body = json.dumps(message, ensure_ascii=False).encode('utf-8')
    properties = pika.BasicProperties(
        content_type='application/json',
        delivery_mode=2 if durable else 1,
        correlation_id=correlation_id,
        reply_to=reply_to,
        message_id=str(uuid.uuid4()),
    )

    channel.basic_publish(
        exchange=exchange,
        routing_key=routing_key,
        body=body,
        properties=properties,
    )
    connection.close()


class RabbitMQEventPublisher:
    """One-way event publisher for RabbitMQ."""

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 5672,
        queue: str | None = None,
        exchange: str = '',
        exchange_type: str = 'direct',
        routing_key: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._queue = queue
        self._exchange = exchange
        self._exchange_type = exchange_type
        self._routing_key = routing_key
        self._username = username
        self._password = password

    def publish(self, event: dict[str, Any]) -> None:
        """Publish one event."""
        if self._queue:
            params = _default_params(
                host=self._host,
                port=self._port,
                username=self._username,
                password=self._password,
            )
            connection = pika.BlockingConnection(params)
            channel: BlockingChannel = connection.channel()
            channel.queue_declare(queue=self._queue, durable=True)
            body = json.dumps(event, ensure_ascii=False).encode('utf-8')
            channel.basic_publish(
                exchange='',
                routing_key=self._queue,
                body=body,
                properties=pika.BasicProperties(
                    content_type='application/json',
                    delivery_mode=2,
                    message_id=str(uuid.uuid4()),
                ),
            )
            connection.close()
            return

        publish_json_message(
            host=self._host,
            port=self._port,
            exchange=self._exchange,
            exchange_type=self._exchange_type,
            routing_key=self._routing_key or '',
            message=event,
            username=self._username,
            password=self._password,
        )

    def close(self) -> None:
        """No-op; each publish opens and closes its own connection."""
        return None


class RabbitMQRPCClient:
    """Direct-exchange RPC client for connector commands."""

    def __init__(
        self,
        host: str = 'localhost',
        port: int = 5672,
        commands_exchange: str = 'aurelion.connectors.commands',
        responses_exchange: str = 'aurelion.connectors.responses',
        client_id: str | None = None,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: int = 60,
    ) -> None:
        self._commands_exchange = commands_exchange
        self._responses_exchange = responses_exchange
        self._timeout_seconds = timeout_seconds
        self._client_id = client_id or str(uuid.uuid4())
        self._reply_queue = f'aurelion.api.rpc.{self._client_id}.replies'
        self._responses: dict[str, dict[str, Any]] = {}

        params = _default_params(host=host, port=port, username=username, password=password)
        self._connection = pika.BlockingConnection(params)
        self._channel: BlockingChannel = self._connection.channel()

        self._channel.exchange_declare(
            exchange=self._commands_exchange,
            exchange_type='direct',
            durable=True,
        )
        self._channel.exchange_declare(
            exchange=self._responses_exchange,
            exchange_type='direct',
            durable=True,
        )
        self._channel.queue_declare(
            queue=self._reply_queue,
            durable=False,
            exclusive=True,
            auto_delete=True,
        )
        self._channel.queue_bind(
            queue=self._reply_queue,
            exchange=self._responses_exchange,
            routing_key=self._client_id,
        )
        self._channel.basic_consume(
            queue=self._reply_queue,
            on_message_callback=self._on_response,
            auto_ack=True,
        )

    def _on_response(
        self,
        _ch: BlockingChannel,
        _method: Any,
        props: Any,
        body: bytes,
    ) -> None:
        if props.correlation_id is None:
            return
        payload = json.loads(body.decode('utf-8'))
        if isinstance(payload, dict):
            self._responses[props.correlation_id] = payload

    async def request(
        self,
        *,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        result_storage_requested: bool = False,
        correlation_id: str | None = None,
        trace_parent_event_id: str | None = None,
        trace_initiator_type: str | None = None,
        trace_initiator_id: str | None = None,
        trace_target_type: str | None = None,
        trace_target_id: str | None = None,
    ) -> dict[str, Any]:
        return self._request_sync(
            instance_id=instance_id,
            operation=operation,
            payload=payload,
            result_storage_requested=result_storage_requested,
            correlation_id=correlation_id or str(uuid.uuid4()),
            trace_parent_event_id=trace_parent_event_id,
            trace_initiator_type=trace_initiator_type,
            trace_initiator_id=trace_initiator_id,
            trace_target_type=trace_target_type,
            trace_target_id=trace_target_id,
        )

    def _request_sync(
        self,
        *,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        result_storage_requested: bool,
        correlation_id: str,
        trace_parent_event_id: str | None = None,
        trace_initiator_type: str | None = None,
        trace_initiator_id: str | None = None,
        trace_target_type: str | None = None,
        trace_target_id: str | None = None,
    ) -> dict[str, Any]:
        message = {
            'correlation_id': correlation_id,
            'reply_exchange': self._responses_exchange,
            'reply_routing_key': self._client_id,
            'operation': operation,
            'result_storage_requested': result_storage_requested,
            'payload': payload,
        }
        if trace_parent_event_id is not None:
            message['trace_parent_event_id'] = trace_parent_event_id
            message['trace_initiator_type'] = trace_initiator_type
            message['trace_initiator_id'] = trace_initiator_id
            message['trace_target_type'] = trace_target_type
            message['trace_target_id'] = trace_target_id

        body = json.dumps(message, ensure_ascii=False).encode('utf-8')
        self._channel.basic_publish(
            exchange=self._commands_exchange,
            routing_key=instance_id,
            body=body,
            properties=pika.BasicProperties(
                content_type='application/json',
                delivery_mode=2,
                correlation_id=correlation_id,
                message_id=str(uuid.uuid4()),
            ),
        )

        started = time.monotonic()
        while correlation_id not in self._responses:
            self._connection.process_data_events(time_limit=1)
            if time.monotonic() - started >= self._timeout_seconds:
                raise TimeoutError(f'RPC timeout for operation={operation!r} instance_id={instance_id!r}')

        return self._responses.pop(correlation_id)

    def close(self) -> None:
        """Close the connection."""
        if self._connection.is_open:
            self._connection.close()


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
        except Exception:
            _ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            return

        _ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=queue_name, on_message_callback=_callback)

    try:
        channel.start_consuming()
    finally:
        connection.close()
