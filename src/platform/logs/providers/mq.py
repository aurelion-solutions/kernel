# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RabbitMQ-backed log sink."""

import json

from src.core.mq.async_publisher import AsyncRabbitMQPublisher
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent


def _sanitize_component(value: str) -> str:
    return value.strip().replace(' ', '-').replace('/', '-')


class RabbitMQLogSink(LogSink):
    """Publish structured logs into RabbitMQ.

    Accepts an :class:`~src.core.mq.async_publisher.AsyncRabbitMQPublisher`
    that is shared across the application lifetime (created in lifespan).
    The ``exchange`` name is injected by the composition root (``settings.rabbitmq_logs_exchange``).
    """

    def __init__(self, publisher: AsyncRabbitMQPublisher, *, exchange: str) -> None:
        self._publisher = publisher
        self._exchange = exchange

    async def emit(self, event: LogEvent) -> None:
        routing_key = f'{_sanitize_component(event.component)}.{event.level.value}'
        body = json.dumps(event.model_dump(mode='json'), ensure_ascii=False).encode('utf-8')
        await self._publisher.publish(
            exchange=self._exchange,
            exchange_type='topic',
            routing_key=routing_key,
            body=body,
        )
