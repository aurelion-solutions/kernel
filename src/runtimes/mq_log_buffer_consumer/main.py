# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""
Consume from the log buffer queue and persist normalized LogEvent v2 rows to PostgreSQL.

Declares the same fan-out topology as the SIEM consumer so either process can start first.
"""

import os
from typing import Any

from dotenv import load_dotenv

# load_dotenv MUST precede all src.* imports that may trigger get_settings()
load_dotenv()

# ruff: noqa: E402
from src.platform.secrets.factory import register_default_providers

register_default_providers()
import pika
from pika.adapters.blocking_connection import BlockingChannel
from src.core.config import get_settings
from src.core.db.session import get_session_factory
from src.core.mq.rabbitmq import declare_topic_exchange_fanout_queues
from src.platform.logs.buffer_consumer import buffer_queue_callback
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_binding_keys(raw: str | None) -> list[str]:
    if not raw:
        return ['#']
    return [item.strip() for item in raw.split(',') if item.strip()]


def main() -> None:
    settings = get_settings()
    mq = settings.rabbitmq
    host = mq.host
    port = mq.port
    username = mq.username
    password = mq.password.get_secret_value()

    exchange = mq.logs_exchange
    siem_queue = _str_env('AURELION_LOGS_QUEUE', 'aurelion.logs.siem')
    buffer_queue = _str_env('AURELION_LOGS_BUFFER_QUEUE', 'aurelion.logs.buffer')
    binding_keys = _parse_binding_keys(os.environ.get('AURELION_LOGS_BINDINGS'))

    params = pika.ConnectionParameters(
        host=host,
        port=port,
        credentials=pika.PlainCredentials(username=username, password=password),
    )
    connection = pika.BlockingConnection(params)
    channel: BlockingChannel = connection.channel()

    topology_queues = list(dict.fromkeys([siem_queue, buffer_queue]))
    declare_topic_exchange_fanout_queues(
        channel,
        exchange=exchange,
        queue_names=topology_queues,
        binding_keys=binding_keys,
    )

    session_factory = get_session_factory()

    def _callback(
        _ch: BlockingChannel,
        method: Any,
        props: Any,
        body: bytes,
    ) -> None:
        buffer_queue_callback(_ch, method, props, body, session_factory=session_factory)

    channel.basic_consume(queue=buffer_queue, on_message_callback=_callback)

    # Bootstrap log to stderr via structlog-compatible output
    from src.platform.logs.factory import log_sink_factory  # noqa: PLC0415

    log_service = LogService(sink=log_sink_factory.get('file'))
    log_service.emit_safe(
        level=LogLevel.INFO,
        message=f'Starting MQ log buffer consumer (Postgres): {host}:{port} exchange={exchange} queue={buffer_queue}',
        component='mq.log.buffer.consumer',
        payload={
            'initiator_type': 'system',
            'initiator_id': 'platform',
            'actor_type': 'system',
            'actor_id': 'mq.log.buffer.consumer',
            'target_type': 'system',
            'target_id': 'log_event_buffer',
        },
    )

    try:
        channel.start_consuming()
    finally:
        connection.close()


if __name__ == '__main__':
    main()
