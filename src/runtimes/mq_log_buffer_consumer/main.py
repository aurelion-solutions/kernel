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
from src.core.db.session import SessionLocal
from src.core.mq.rabbitmq import declare_topic_exchange_fanout_queues
from src.platform.logs.buffer_consumer import buffer_queue_callback

load_dotenv()


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_binding_keys(raw: str | None) -> list[str]:
    if not raw:
        return ['#']
    return [item.strip() for item in raw.split(',') if item.strip()]


def main() -> None:
    host = _str_env('AURELION_RABBITMQ_HOST', 'localhost')
    port = _int_env('AURELION_RABBITMQ_PORT', 5672)
    username = os.environ.get('AURELION_RABBITMQ_USERNAME') or None
    password = os.environ.get('AURELION_RABBITMQ_PASSWORD') or None

    exchange = _str_env('AURELION_LOGS_EXCHANGE', 'aurelion.logs')
    siem_queue = _str_env('AURELION_LOGS_QUEUE', 'aurelion.logs.siem')
    buffer_queue = _str_env('AURELION_LOGS_BUFFER_QUEUE', 'aurelion.logs.buffer')
    binding_keys = _parse_binding_keys(os.environ.get('AURELION_LOGS_BINDINGS'))

    user = username if username is not None else 'guest'
    passwd = password if password is not None else 'guest'
    params = pika.ConnectionParameters(
        host=host,
        port=port,
        credentials=pika.PlainCredentials(username=user, password=passwd),
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
