# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Truly async RabbitMQ publisher backed by aio-pika RobustConnection."""

import asyncio
import logging
import os
from typing import Any

import aio_pika
import aio_pika.abc

logger = logging.getLogger(__name__)


def _rabbitmq_url() -> str:
    host = os.environ.get('AURELION_RABBITMQ_HOST', 'localhost')
    port = os.environ.get('AURELION_RABBITMQ_PORT', '5672')
    username = os.environ.get('AURELION_RABBITMQ_USERNAME', 'guest')
    password = os.environ.get('AURELION_RABBITMQ_PASSWORD', 'guest')
    return f'amqp://{username}:{password}@{host}:{port}/'


class AsyncRabbitMQPublisher:
    """Persistent async RabbitMQ publisher with publisher confirms and retry.

    Uses ``aio_pika.connect_robust`` so the connection auto-reconnects on
    transient failures without caller intervention.

    Usage::

        publisher = AsyncRabbitMQPublisher()
        await publisher.connect()
        await publisher.publish(
            exchange='aurelion.events',
            exchange_type='topic',
            routing_key='inventory.employee.created',
            body=b'...',
        )
        await publisher.close()
    """

    _MAX_ATTEMPTS = 3
    _RETRY_DELAYS = (0.5, 1.0, 2.0)  # seconds between attempt 1→2, 2→3

    def __init__(self, url: str | None = None) -> None:
        self._url = url if url is not None else _rabbitmq_url()
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        # Cache of already-declared exchange names → aio_pika.Exchange objects
        self._exchanges: dict[str, aio_pika.abc.AbstractExchange] = {}

    async def connect(self) -> None:
        """Open a robust connection and a confirm-delivery channel."""
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=0)
        self._exchanges.clear()

    async def close(self) -> None:
        """Close channel and connection gracefully."""
        if self._channel is not None:
            try:
                await self._channel.close()
            except Exception:
                pass
            self._channel = None

        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:
                pass
            self._connection = None

        self._exchanges.clear()

    async def _get_exchange(
        self,
        exchange: str,
        exchange_type: str,
    ) -> aio_pika.abc.AbstractExchange:
        """Return a cached exchange handle, declaring it if necessary."""
        if exchange not in self._exchanges:
            assert self._channel is not None, 'Publisher not connected'
            exc = await self._channel.declare_exchange(
                exchange,
                type=aio_pika.ExchangeType(exchange_type),
                durable=True,
            )
            self._exchanges[exchange] = exc
        return self._exchanges[exchange]

    async def publish(
        self,
        *,
        exchange: str,
        exchange_type: str,
        routing_key: str,
        body: bytes,
        headers: dict[str, Any] | None = None,
    ) -> None:
        """Publish ``body`` to ``exchange`` with ``routing_key``.

        Retries up to ``_MAX_ATTEMPTS`` times with exponential back-off on
        any exception.  On exhaustion the last exception is re-raised.
        """
        last_exc: BaseException | None = None
        for attempt in range(self._MAX_ATTEMPTS):
            try:
                exc_obj = await self._get_exchange(exchange, exchange_type)
                message = aio_pika.Message(
                    body=body,
                    content_type='application/json',
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    headers=headers or {},
                )
                await exc_obj.publish(message, routing_key=routing_key)
                return
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    'AsyncRabbitMQPublisher publish attempt %d/%d failed: %s',
                    attempt + 1,
                    self._MAX_ATTEMPTS,
                    exc,
                )
                # Invalidate cached exchange so we re-declare on next attempt
                self._exchanges.pop(exchange, None)
                if attempt < self._MAX_ATTEMPTS - 1:
                    await asyncio.sleep(self._RETRY_DELAYS[attempt])

        assert last_exc is not None
        raise last_exc
