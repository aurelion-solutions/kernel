# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RabbitMQ-backed event sink for the ``aurelion.events`` topic exchange."""

import json
import os

from src.core.mq.async_publisher import AsyncRabbitMQPublisher
from src.platform.events.schemas import EventEnvelope


class RabbitMQEventSink:
    """Publish domain events to RabbitMQ on the ``aurelion.events`` topic exchange.

    Accepts an :class:`~src.core.mq.async_publisher.AsyncRabbitMQPublisher`
    that is shared across the application lifetime (created in lifespan).
    Routing key == ``event.event_type`` (no transformation; validator enforces grammar).
    Re-raises any publish / network error — callers decide on reliability policy.
    """

    def __init__(self, publisher: AsyncRabbitMQPublisher) -> None:
        self._publisher = publisher

    async def emit(self, event: EventEnvelope) -> None:
        exchange = os.environ.get('AURELION_EVENTS_EXCHANGE', 'aurelion.events')
        body = json.dumps(event.model_dump(mode='json'), ensure_ascii=False).encode('utf-8')
        await self._publisher.publish(
            exchange=exchange,
            exchange_type='topic',
            routing_key=event.event_type,
            body=body,
        )
