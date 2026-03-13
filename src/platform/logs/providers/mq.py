# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RabbitMQ-backed log sink."""

import os

from src.core.mq.rabbitmq import publish_json_message
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent


def _sanitize_component(value: str) -> str:
    return value.strip().replace(' ', '-').replace('/', '-')


class RabbitMQLogSink(LogSink):
    """Publish structured logs into RabbitMQ."""

    def emit(self, event: LogEvent) -> None:
        host = os.environ.get('AURELION_RABBITMQ_HOST', 'localhost')
        port = int(os.environ.get('AURELION_RABBITMQ_PORT', '5672'))
        username = os.environ.get('AURELION_RABBITMQ_USERNAME')
        password = os.environ.get('AURELION_RABBITMQ_PASSWORD')
        exchange = os.environ.get('AURELION_LOGS_EXCHANGE', 'aurelion.logs')

        routing_key = f'{_sanitize_component(event.component)}.{event.level.value}'
        payload = event.model_dump(mode='json')

        publish_json_message(
            host=host,
            port=port,
            exchange=exchange,
            exchange_type='topic',
            routing_key=routing_key,
            message=payload,
            username=username,
            password=password,
        )
