# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
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


def run_connector_registration_consumer(
    main_loop: asyncio.AbstractEventLoop,
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    registration_exchange: str,
    registration_queue: str,
    registration_binding_keys: list[str],
) -> None:
    """Block forever: consume connector registration / heartbeat JSON from RabbitMQ.

    DB work runs on *main_loop* (uvicorn's loop) via run_coroutine_threadsafe so asyncpg
    shares the same event loop as the AsyncEngine; a per-thread loop would break pool
    teardown on shutdown.

    All connection and topology parameters are passed explicitly by the caller
    (composition root).  This function does not read environment variables.
    """

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
        exchange=registration_exchange,
        exchange_type='topic',
        queue_name=registration_queue,
        binding_keys=registration_binding_keys,
        username=username,
        password=password,
    )
