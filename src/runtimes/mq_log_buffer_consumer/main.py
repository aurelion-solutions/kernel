# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""
Consume from the log buffer queue and persist normalized LogEvent v2 rows to PostgreSQL.

Declares the same fan-out topology as the SIEM consumer so either process can start first.
"""

import os
import sys
from typing import Any

from dotenv import load_dotenv
import pika
from pika.adapters.blocking_connection import BlockingChannel
from src.core.config import settings
from src.core.db.session import SessionLocal
from src.core.mq.rabbitmq import declare_topic_exchange_fanout_queues
from src.platform.logs.buffer_consumer import buffer_queue_callback

load_dotenv()


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_binding_keys(raw: str | None) -> list[str]:
    if not raw:
        return ['#']
    return [item.strip() for item in raw.split(',') if item.strip()]


def main() -> None:
    host = settings.rabbitmq_host
    port = settings.rabbitmq_port
    username = settings.rabbitmq_username
    password = settings.rabbitmq_password

    exchange = settings.rabbitmq_logs_exchange
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

    def _callback(
        _ch: BlockingChannel,
        method: Any,
        props: Any,
        body: bytes,
    ) -> None:
        buffer_queue_callback(_ch, method, props, body, session_factory=SessionLocal)

    channel.basic_consume(queue=buffer_queue, on_message_callback=_callback)

    print(
        f'Starting MQ log buffer consumer (Postgres): {host}:{port} exchange={exchange} queue={buffer_queue}',
        file=sys.stderr,
    )

    try:
        channel.start_consuming()
    finally:
        connection.close()


if __name__ == '__main__':
    main()
