# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Entrypoint for the MQ EAS incremental projection consumer.

Run from the aurelion-kernel root:
    python -m src.runtimes.mq_eas_projection_consumer.main

Env vars (bootstrap-driven — set via AURELION_SECRET_PROVIDER / AURELION_SECRETS_FILE):
    postgres    (secret key)
    rabbitmq    (secret key)

Env vars (still read directly — out of Phase 11 Step 1 scope):
    AURELION_EAS_PROJECTION_QUEUE    (default: eas.projection.incremental)
    AURELION_EAS_PROJECTION_BINDINGS (default: inventory.access_fact.*,inventory.initiative.*)
    AURELION_LOG_SINK_PROVIDER       (default: file)
    AURELION_EVENTS_PROVIDER         (default: mq)
"""

from __future__ import annotations

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
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.config import get_settings
from src.core.db.session import get_session_factory
from src.core.mq.rabbitmq import declare_consumer_topology
from src.engines.effective_access.service import EffectiveAccessProjectionService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.runtimes.mq_eas_projection_consumer.handler import handle_message

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _str_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def _parse_binding_keys(raw: str | None) -> list[str]:
    if not raw:
        return ['inventory.access_fact.*', 'inventory.initiative.*']
    return [item.strip() for item in raw.split(',') if item.strip()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    settings = get_settings()
    mq = settings.rabbitmq
    host = mq.host
    port = mq.port
    username = mq.username
    password = mq.password.get_secret_value()

    exchange = mq.events_exchange
    queue = _str_env('AURELION_EAS_PROJECTION_QUEUE', 'eas.projection.incremental')
    bindings_raw = os.environ.get('AURELION_EAS_PROJECTION_BINDINGS')
    bindings = _parse_binding_keys(bindings_raw)
    sink_provider = _str_env('AURELION_LOG_SINK_PROVIDER', 'file')

    log_service = LogService(sink=log_sink_factory.get(sink_provider))
    event_service = EventService(sink=event_sink_factory.get(_get_events_provider()))

    session_factory = get_session_factory()

    def projection_service_factory(
        session: AsyncSession, event_service: EventService
    ) -> EffectiveAccessProjectionService:
        return EffectiveAccessProjectionService(session, event_service=event_service)

    credentials = pika.PlainCredentials(username=username, password=password)
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
        level=LogLevel.INFO,
        message=f'Starting MQ EAS projection consumer: {host}:{port} events_exchange={exchange} queue={queue}',
        component='eas.projection.consumer',
        payload={
            'initiator_type': 'system',
            'initiator_id': 'platform',
            'actor_type': 'system',
            'actor_id': 'eas.projection.consumer',
            'target_type': 'system',
            'target_id': 'eas_projection',
            'host': host,
            'port': port,
            'events_exchange': exchange,
            'queue': queue,
            'bindings': bindings,
        },
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
                routing_key=method.routing_key,
                session_factory=session_factory,
                projection_service_factory=projection_service_factory,
                log_service=log_service,
                event_service=event_service,
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
