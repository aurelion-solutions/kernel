# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
import os
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker
from src.core.db.session import SessionLocal
from src.core.mq.rabbitmq import run_rabbitmq_consumer
from src.platform.connectors.registration_schemas import ConnectorRegistrationMessage
from src.platform.connectors.service import ConnectorInstanceService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_capability_trace_fields, noop_log_service


async def handle_connector_registration(
    session_factory: async_sessionmaker,
    message: dict,
    log_service: LogService | None = None,
) -> None:
    log = log_service if log_service is not None else noop_log_service

    try:
        payload = ConnectorRegistrationMessage.model_validate(message)
    except ValidationError as exc:
        # NOTE: kwarg-shape refactor (Step 23 Phase 10) — NOT a migration to aurelion.events bus.
        log.emit_safe(
            level=LogLevel.ERROR,
            message='Invalid connector registration message',
            component='connectors',
            payload=merge_emit_capability_trace_fields(
                {'errors': exc.errors()},
                capability_id='connectors',
                target_id='registration',
            ),
        )
        raise

    service = ConnectorInstanceService()

    async with session_factory() as session:
        await service.register_from_message(
            session,
            instance_id=payload.instance_id,
            tags=payload.tags,
            log_service=log,
        )
        await session.commit()


def run_connector_registration_consumer(main_loop: asyncio.AbstractEventLoop) -> None:
    """Block forever: consume connector registration / heartbeat JSON from RabbitMQ.

    DB work runs on *main_loop* (uvicorn's loop) via run_coroutine_threadsafe so asyncpg
    shares the same event loop as the AsyncEngine; a per-thread loop would break pool
    teardown on shutdown.
    """
    host = os.environ.get('AURELION_RABBITMQ_HOST', 'localhost')
    port = int(os.environ.get('AURELION_RABBITMQ_PORT', '5672'))
    username = os.environ.get('AURELION_RABBITMQ_USERNAME')
    password = os.environ.get('AURELION_RABBITMQ_PASSWORD')
    exchange = os.environ.get(
        'AURELION_CONNECTOR_REGISTRATION_EXCHANGE',
        'aurelion.connectors.registry',
    )
    queue_name = os.environ.get(
        'AURELION_CONNECTOR_REGISTRATION_QUEUE',
        'aurelion.connectors.registration',
    )
    binding_raw = os.environ.get(
        'AURELION_CONNECTOR_REGISTRATION_BINDINGS',
        'connector.registered,connector.heartbeat',
    )
    binding_keys = [k.strip() for k in binding_raw.split(',') if k.strip()]

    def on_event(raw: dict[str, Any], _routing_key: str, _props: Any) -> None:
        fut = asyncio.run_coroutine_threadsafe(
            handle_connector_registration(SessionLocal, raw),
            main_loop,
        )
        fut.result()

    run_rabbitmq_consumer(
        on_event=on_event,
        host=host,
        port=port,
        exchange=exchange,
        exchange_type='topic',
        queue_name=queue_name,
        binding_keys=binding_keys,
        username=username,
        password=password,
    )
