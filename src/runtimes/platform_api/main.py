# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
from contextlib import asynccontextmanager
import os
import threading

from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()
# ruff: noqa: E402
from fastapi.middleware.cors import CORSMiddleware
from src.core.config import settings
from src.core.db.session import engine
from src.core.mq.async_publisher import AsyncRabbitMQPublisher
from src.core.mq.async_rpc_client import AsyncRabbitMQRPCClient
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.registration_consumer import run_connector_registration_consumer
from src.platform.events.buffer import InMemoryEventBuffer, InMemoryEventBufferSink
from src.platform.events.factory import event_sink_factory
from src.platform.events.providers.mq import RabbitMQEventSink
from src.platform.events.tee_sink import TeeEventSink
from src.platform.logs.providers.mq import RabbitMQLogSink
from src.platform.logs.service import LogService
from src.routers.v0 import router as v0_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    url = settings.rabbitmq_url

    publisher = AsyncRabbitMQPublisher(url=url)
    await publisher.connect()

    # In-memory event ring buffer for the IDE observability panel
    event_buffer = InMemoryEventBuffer()
    app.state.event_buffer = event_buffer

    # Wire up MQ-backed primary sink + in-memory tap via TeeEventSink
    event_sink_factory.register(
        'mq',
        lambda: TeeEventSink(
            RabbitMQEventSink(publisher, exchange=settings.rabbitmq_events_exchange),
            InMemoryEventBufferSink(event_buffer),
        ),
    )
    log_sink = RabbitMQLogSink(publisher, exchange=settings.rabbitmq_logs_exchange)
    app.state.log_service = LogService(sink=log_sink)

    rpc_client = AsyncRabbitMQRPCClient(
        url=url,
        commands_exchange=settings.rabbitmq_connector_commands_exchange,
        responses_exchange=settings.rabbitmq_connector_responses_exchange,
    )
    await rpc_client.connect()

    app.state.publisher = publisher
    app.state.rpc_client = rpc_client
    app.state.connector_client = ConnectorClient(rpc_client=rpc_client)

    # Read registration topology from env (out of Phase 11 Step 1 Settings field list).
    registration_exchange = os.environ.get(
        'AURELION_CONNECTOR_REGISTRATION_EXCHANGE',
        'aurelion.connectors.registry',
    )
    registration_queue = os.environ.get(
        'AURELION_CONNECTOR_REGISTRATION_QUEUE',
        'aurelion.connectors.registration',
    )
    registration_binding_raw = os.environ.get(
        'AURELION_CONNECTOR_REGISTRATION_BINDINGS',
        'connector.registered,connector.heartbeat',
    )
    registration_binding_keys = [k.strip() for k in registration_binding_raw.split(',') if k.strip()]

    main_loop = asyncio.get_running_loop()
    connector_registration_thread = threading.Thread(
        target=run_connector_registration_consumer,
        args=(main_loop,),
        kwargs={
            'host': settings.rabbitmq_host,
            'port': settings.rabbitmq_port,
            'username': settings.rabbitmq_username,
            'password': settings.rabbitmq_password,
            'registration_exchange': registration_exchange,
            'registration_queue': registration_queue,
            'registration_binding_keys': registration_binding_keys,
        },
        daemon=True,
    )
    connector_registration_thread.start()

    yield

    await rpc_client.close()
    await publisher.close()
    await engine.dispose()


app = FastAPI(lifespan=lifespan, title='Aurelion Platform API')

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/health')
async def health() -> dict:
    return {'status': 'ok'}


app.include_router(v0_router, prefix='/api/v0')
