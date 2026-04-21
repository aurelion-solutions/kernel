# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Truly async RabbitMQ RPC client backed by aio-pika."""

import asyncio
import json
import logging
from typing import Any
import uuid

import aio_pika
import aio_pika.abc
from src.core.mq.async_publisher import _rabbitmq_url

logger = logging.getLogger(__name__)


class AsyncRabbitMQRPCClient:
    """Async RPC client for connector commands over RabbitMQ.

    Maintains a persistent connection.  Reply messages are dispatched via
    ``asyncio.Future`` so ``request()`` truly suspends the coroutine
    instead of polling.

    Usage::

        client = AsyncRabbitMQRPCClient(url=...)
        await client.connect()
        result = await client.request(instance_id='...', operation='...', payload={})
        await client.close()
    """

    def __init__(
        self,
        url: str | None = None,
        commands_exchange: str = 'aurelion.connectors.commands',
        responses_exchange: str = 'aurelion.connectors.responses',
        timeout_seconds: int = 60,
    ) -> None:
        self._url = url if url is not None else _rabbitmq_url()
        self._commands_exchange = commands_exchange
        self._responses_exchange = responses_exchange
        self._timeout_seconds = timeout_seconds
        self._client_id = str(uuid.uuid4())
        self._reply_queue_name = f'aurelion.api.rpc.{self._client_id}.replies'

        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._cmd_exchange: aio_pika.abc.AbstractExchange | None = None
        # correlation_id → Future that resolves when the reply arrives
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

    async def connect(self) -> None:
        """Open connection, declare topology, start consuming replies."""
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()

        # Commands exchange (direct, durable)
        self._cmd_exchange = await self._channel.declare_exchange(
            self._commands_exchange,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
        )

        # Responses exchange (direct, durable)
        resp_exchange = await self._channel.declare_exchange(
            self._responses_exchange,
            type=aio_pika.ExchangeType.DIRECT,
            durable=True,
        )

        # Exclusive, auto-delete reply queue — non-durable (ephemeral per client)
        reply_queue = await self._channel.declare_queue(
            self._reply_queue_name,
            durable=False,
            exclusive=True,
            auto_delete=True,
        )
        await reply_queue.bind(resp_exchange, routing_key=self._client_id)
        await reply_queue.consume(self._on_reply, no_ack=True)

    async def close(self) -> None:
        """Cancel pending futures, close channel and connection."""
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

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

    async def _on_reply(self, message: aio_pika.abc.AbstractIncomingMessage) -> None:
        """Resolve the pending Future for ``correlation_id``."""
        correlation_id = message.correlation_id
        if not correlation_id:
            logger.warning('RPC reply arrived with no correlation_id — discarded')
            return

        fut = self._pending.get(correlation_id)
        if fut is None:
            logger.warning('RPC reply for unknown correlation_id=%r — discarded', correlation_id)
            return

        try:
            payload = json.loads(message.body.decode('utf-8'))
        except Exception as exc:
            if not fut.done():
                fut.set_exception(exc)
            return

        if not fut.done():
            fut.set_result(payload if isinstance(payload, dict) else {'_raw': payload})

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
        """Send an RPC command and await the reply.

        Raises ``TimeoutError`` if no reply arrives within ``timeout_seconds``.
        """
        assert self._cmd_exchange is not None, 'RPC client not connected'

        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[cid] = fut

        body_dict: dict[str, Any] = {
            'correlation_id': cid,
            'reply_exchange': self._responses_exchange,
            'reply_routing_key': self._client_id,
            'operation': operation,
            'result_storage_requested': result_storage_requested,
            'payload': payload,
        }
        if trace_parent_event_id is not None:
            body_dict['trace_parent_event_id'] = trace_parent_event_id
            body_dict['trace_initiator_type'] = trace_initiator_type
            body_dict['trace_initiator_id'] = trace_initiator_id
            body_dict['trace_target_type'] = trace_target_type
            body_dict['trace_target_id'] = trace_target_id

        message = aio_pika.Message(
            body=json.dumps(body_dict, ensure_ascii=False).encode('utf-8'),
            content_type='application/json',
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            correlation_id=cid,
            message_id=str(uuid.uuid4()),
        )

        try:
            await self._cmd_exchange.publish(message, routing_key=instance_id)
            result = await asyncio.wait_for(fut, timeout=float(self._timeout_seconds))
        except TimeoutError as exc:
            self._pending.pop(cid, None)
            raise TimeoutError(f'RPC timeout for operation={operation!r} instance_id={instance_id!r}') from exc
        except Exception:
            self._pending.pop(cid, None)
            raise
        else:
            self._pending.pop(cid, None)

        return result
