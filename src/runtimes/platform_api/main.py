# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
from contextlib import asynccontextmanager
import os
import threading

from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()
# ruff: noqa: E402
from fastapi.middleware.cors import CORSMiddleware
from src.core.config import settings
from src.core.db.session import engine
from src.core.mq.async_publisher import AsyncRabbitMQPublisher
from src.core.mq.async_rpc_client import AsyncRabbitMQRPCClient
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.registration_consumer import run_connector_registration_consumer
from src.platform.events.factory import event_sink_factory
from src.platform.events.providers.mq import RabbitMQEventSink
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.providers.mq import RabbitMQLogSink
from src.routers.v0 import router as v0_router


def _rabbitmq_url() -> str:
    host = os.environ.get('AURELION_RABBITMQ_HOST', 'localhost')
    port = os.environ.get('AURELION_RABBITMQ_PORT', '5672')
    username = os.environ.get('AURELION_RABBITMQ_USERNAME', 'guest')
    password = os.environ.get('AURELION_RABBITMQ_PASSWORD', 'guest')
    return f'amqp://{username}:{password}@{host}:{port}/'


@asynccontextmanager
async def lifespan(app: FastAPI):
    url = _rabbitmq_url()

    publisher = AsyncRabbitMQPublisher(url=url)
    await publisher.connect()

    # Wire up MQ-backed sinks with the shared publisher
    event_sink_factory.register('mq', lambda: RabbitMQEventSink(publisher))
    log_sink_factory.register('mq', lambda: RabbitMQLogSink(publisher))

    commands_exchange = os.environ.get(
        'AURELION_CONNECTOR_COMMANDS_EXCHANGE',
        'aurelion.connectors.commands',
    )
    responses_exchange = os.environ.get(
        'AURELION_CONNECTOR_RESPONSES_EXCHANGE',
        'aurelion.connectors.responses',
    )
    rpc_client = AsyncRabbitMQRPCClient(
        url=url,
        commands_exchange=commands_exchange,
        responses_exchange=responses_exchange,
    )
    await rpc_client.connect()

    app.state.publisher = publisher
    app.state.rpc_client = rpc_client
    app.state.connector_client = ConnectorClient(rpc_client=rpc_client)

    main_loop = asyncio.get_running_loop()
    connector_registration_thread = threading.Thread(
        target=run_connector_registration_consumer,
        args=(main_loop,),
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


@app.middleware('http')
async def attach_log_service(request: Request, call_next):
    from src.platform.logs.service import LogService

    request.state.log_service = LogService(factory=log_sink_factory)
    response = await call_next(request)
    return response


app.include_router(v0_router, prefix='/api/v0')
