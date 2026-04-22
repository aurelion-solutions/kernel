# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RabbitMQ-backed event sink for the ``aurelion.events`` topic exchange."""

import json

from src.core.mq.async_publisher import AsyncRabbitMQPublisher
from src.platform.events.schemas import EventEnvelope


class RabbitMQEventSink:
    """Publish domain events to RabbitMQ on the ``aurelion.events`` topic exchange.

    Accepts an :class:`~src.core.mq.async_publisher.AsyncRabbitMQPublisher`
    that is shared across the application lifetime (created in lifespan).
    The ``exchange`` name is injected by the composition root (``settings.rabbitmq_events_exchange``).
    Routing key == ``event.event_type`` (no transformation; validator enforces grammar).
    Re-raises any publish / network error — callers decide on reliability policy.
    """

    def __init__(self, publisher: AsyncRabbitMQPublisher, *, exchange: str) -> None:
        self._publisher = publisher
        self._exchange = exchange

    async def emit(self, event: EventEnvelope) -> None:
        body = json.dumps(event.model_dump(mode='json'), ensure_ascii=False).encode('utf-8')
        await self._publisher.publish(
            exchange=self._exchange,
            exchange_type='topic',
            routing_key=event.event_type,
            body=body,
        )
