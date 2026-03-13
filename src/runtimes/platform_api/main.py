# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
from contextlib import asynccontextmanager
import threading

from dotenv import load_dotenv
from fastapi import FastAPI, Request

load_dotenv()
# ruff: noqa: E402
from fastapi.middleware.cors import CORSMiddleware
from src.core.db.session import engine
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.registration_consumer import run_connector_registration_consumer
from src.routers.v0 import router as v0_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ConnectorClient is RPC + lake only; instance_id comes from ConnectorInstanceService
    # using the request-scoped DB session (see get_db), not from app.state.
    app.state.connector_client = ConnectorClient()

    main_loop = asyncio.get_running_loop()
    connector_registration_thread = threading.Thread(
        target=run_connector_registration_consumer,
        args=(main_loop,),
        daemon=True,
    )
    connector_registration_thread.start()

    yield

    await engine.dispose()


app = FastAPI(lifespan=lifespan, title='Aurelion Platform API')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


@app.get('/health')
async def health() -> dict:
    return {'status': 'ok'}


@app.middleware('http')
async def attach_log_service(request: Request, call_next):
    from src.platform.logs.factory import log_sink_factory
    from src.platform.logs.service import LogService

    request.state.log_service = LogService(factory=log_sink_factory)
    response = await call_next(request)
    return response


app.include_router(v0_router, prefix='/api/v0')
