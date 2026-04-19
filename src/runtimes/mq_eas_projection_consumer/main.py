# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Entrypoint for the MQ EAS incremental projection consumer.

Run from the aurelion-kernel root:
    python -m src.runtimes.mq_eas_projection_consumer.main

Env vars:
    AURELION_RABBITMQ_HOST           (default: localhost)
    AURELION_RABBITMQ_PORT           (default: 5672)
    AURELION_RABBITMQ_USERNAME       (default: guest)
    AURELION_RABBITMQ_PASSWORD       (default: guest)
    AURELION_LOGS_EXCHANGE           (default: aurelion.logs)
    AURELION_EAS_PROJECTION_QUEUE    (default: eas.projection.incremental)
    AURELION_EAS_PROJECTION_BINDINGS (default: inventory.access_facts.*,inventory.initiatives.*)
    AURELION_LOG_SINK_PROVIDER       (default: file)
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
import pika
from pika.adapters.blocking_connection import BlockingChannel
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.effective_access.service import EffectiveAccessProjectionService
from src.core.db.session import SessionLocal
from src.core.mq.rabbitmq import declare_consumer_topology
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.runtimes.mq_eas_projection_consumer.handler import handle_message

load_dotenv()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
        return ['inventory.access_facts.*', 'inventory.initiatives.*']
    return [item.strip() for item in raw.split(',') if item.strip()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    host = _str_env('AURELION_RABBITMQ_HOST', 'localhost')
    port = _int_env('AURELION_RABBITMQ_PORT', 5672)
    username = os.environ.get('AURELION_RABBITMQ_USERNAME') or None
    password = os.environ.get('AURELION_RABBITMQ_PASSWORD') or None

    exchange = _str_env('AURELION_LOGS_EXCHANGE', 'aurelion.logs')
    queue = _str_env('AURELION_EAS_PROJECTION_QUEUE', 'eas.projection.incremental')
    bindings_raw = os.environ.get('AURELION_EAS_PROJECTION_BINDINGS')
    bindings = _parse_binding_keys(bindings_raw)
    sink_provider = _str_env('AURELION_LOG_SINK_PROVIDER', 'file')

    log_service = LogService(factory=log_sink_factory, provider_name=sink_provider)

    def projection_service_factory(session: AsyncSession, ls: LogService) -> EffectiveAccessProjectionService:
        return EffectiveAccessProjectionService(session, ls)

    user = username if username is not None else 'guest'
    passwd = password if password is not None else 'guest'
    credentials = pika.PlainCredentials(username=user, password=passwd)
    params = pika.ConnectionParameters(host=host, port=port, credentials=credentials)
    connection = pika.BlockingConnection(params)
    channel: BlockingChannel = connection.channel()

    declare_consumer_topology(
        channel,
        exchange=exchange,
        exchange_type='topic',
        queue_name=queue,
        binding_keys=bindings,
    )

    log_service.emit_safe(
        'eas.projection.consumer.started',
        LogLevel.INFO,
        f'Starting MQ EAS projection consumer: {host}:{port} exchange={exchange} queue={queue}',
        'eas.projection.consumer',
        {'host': host, 'port': port, 'exchange': exchange, 'queue': queue, 'bindings': bindings},
    )

    def _callback(
        ch: BlockingChannel,
        method: Any,
        _props: Any,
        body: bytes,
    ) -> None:
        try:
            handle_message(
                body,
                session_factory=SessionLocal,
                projection_service_factory=projection_service_factory,
                log_service=log_service,
            )
        finally:
            ch.basic_ack(delivery_tag=method.delivery_tag)

    channel.basic_consume(queue=queue, on_message_callback=_callback)

    try:
        channel.start_consuming()
    finally:
        connection.close()


if __name__ == '__main__':
    main()
