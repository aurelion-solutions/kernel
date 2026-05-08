# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
from contextlib import asynccontextmanager
import os
import threading

from dotenv import load_dotenv

load_dotenv()
# ruff: noqa: E402
from src.platform.secrets.factory import register_default_providers

register_default_providers()
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.core.config import get_settings
from src.core.db.session import get_engine
from src.core.middleware.correlation import CorrelationIdMiddleware
from src.core.mq.async_publisher import AsyncRabbitMQPublisher
from src.core.mq.async_rpc_client import AsyncRabbitMQRPCClient
import src.engines.reconciliation.handlers  # noqa: F401 — bootstrap handler registry
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.registration_consumer import run_connector_registration_consumer
from src.platform.events.buffer import InMemoryEventBuffer, InMemoryEventBufferSink
from src.platform.events.factory import event_sink_factory
from src.platform.events.providers.mq import RabbitMQEventSink
from src.platform.events.tee_sink import TeeEventSink
from src.platform.lake.catalog import get_catalog
from src.platform.lake.config import build_lake_settings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.lake.provisioning import ensure_tables
from src.platform.logs.providers.mq import RabbitMQLogSink
from src.platform.logs.service import LogService, set_main_loop
from src.platform.runtime_settings.service import RuntimeSettingsService
from src.routers.v0 import router as v0_router

# cors_allow_origins lives in bootstrap settings (AppSettings), not RuntimeSettings.
# CORSMiddleware must be registered before the lifespan starts, so origins cannot
# come from the DB.  See AppSettings docstring for the full rationale.
_settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    mq = settings.rabbitmq
    url = mq.url

    set_main_loop(asyncio.get_running_loop())

    publisher = AsyncRabbitMQPublisher(url=url)
    await publisher.connect()

    # In-memory event ring buffer for the IDE observability panel
    event_buffer = InMemoryEventBuffer()
    app.state.event_buffer = event_buffer

    # Wire up MQ-backed primary sink + in-memory tap via TeeEventSink
    event_sink_factory.register(
        'mq',
        lambda: TeeEventSink(
            RabbitMQEventSink(publisher, exchange=mq.events_exchange),
            InMemoryEventBufferSink(event_buffer),
        ),
    )
    log_sink = RabbitMQLogSink(publisher, exchange=mq.logs_exchange)
    log_service = LogService(sink=log_sink)
    app.state.log_service = log_service

    rpc_client = AsyncRabbitMQRPCClient(
        url=url,
        commands_exchange=mq.connector_commands_exchange,
        responses_exchange=mq.connector_responses_exchange,
    )
    await rpc_client.connect()

    app.state.publisher = publisher
    app.state.rpc_client = rpc_client
    app.state.connector_client = ConnectorClient(rpc_client=rpc_client)

    # RuntimeSettings — seed defaults and load typed snapshot
    from src.core.db.session import get_session_factory  # noqa: PLC0415

    async with get_session_factory()() as session:
        rt_service = RuntimeSettingsService(session, log_service)
        await rt_service.ensure_defaults()
        await session.commit()

    async with get_session_factory()() as session:
        rt_service = RuntimeSettingsService(session, log_service)
        runtime = await rt_service.load()

    app.state.runtime_settings = runtime

    # Lake infrastructure
    lake_settings = build_lake_settings(
        settings.postgres,
        runtime,
        catalog_name=settings.lake.catalog_name,
        warehouse_uri=settings.lake.warehouse_uri,
        storage_provider=settings.lake.storage_provider,  # type: ignore[arg-type]
        artifacts_write_backend=settings.lake.artifacts_write_backend,  # type: ignore[arg-type]
    )
    lake_catalog = get_catalog(lake_settings, log_service)
    lake_tables = ensure_tables(lake_catalog, log_service=log_service)
    app.state.lake_tables = lake_tables

    # DuckDB postgres_scanner ATTACH requires a plain postgresql:// DSN (libpq-compatible).
    pg_dsn_for_lake = settings.postgres.dsn.replace('+asyncpg', '').replace('+psycopg2', '')
    lake_session_factory = LakeSessionFactory(
        settings=lake_settings,
        log_service=log_service,
        pg_dsn=pg_dsn_for_lake,
    )
    app.state.lake_settings = lake_settings
    app.state.lake_catalog = lake_catalog
    app.state.lake_session_factory = lake_session_factory

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
            'host': mq.host,
            'port': mq.port,
            'username': mq.username,
            'password': mq.password.get_secret_value(),
            'registration_exchange': registration_exchange,
            'registration_queue': registration_queue,
            'registration_binding_keys': registration_binding_keys,
        },
        daemon=True,
    )
    connector_registration_thread.start()

    yield

    lake_session_factory.close_all()
    await rpc_client.close()
    await publisher.close()
    await get_engine().dispose()
    get_engine.cache_clear()
    get_settings.cache_clear()


app = FastAPI(lifespan=lifespan, title='Aurelion Platform API')

# Middleware registration order: last-registered runs outermost (on the request path).
# Pipeline: CORS (outermost) → CorrelationId → route
app.add_middleware(CorrelationIdMiddleware)  # registered first → inner
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.app.cors_allow_origins,
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)  # registered last → outermost


@app.get('/health')
async def health() -> dict:
    return {'status': 'ok'}


app.include_router(v0_router, prefix='/api/v0')
