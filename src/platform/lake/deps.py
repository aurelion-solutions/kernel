# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the lake infrastructure slice."""

import asyncio
from collections.abc import AsyncIterator

from fastapi import Request
from pyiceberg.catalog import Catalog
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSession


def get_lake_catalog(request: Request) -> Catalog:
    """Return the process-cached Iceberg catalog from app state.

    No per-request lifecycle — the catalog is initialized once in lifespan.
    """
    return request.app.state.lake_catalog  # type: ignore[no-any-return]


async def get_lake_session(request: Request) -> AsyncIterator[LakeSession]:
    """Yield a DuckDB session scoped to the request, released on exit.

    Acquisition is offloaded to a worker thread via ``asyncio.to_thread``
    to avoid blocking the event loop (queue.Queue.get is synchronous).
    """
    factory = request.app.state.lake_session_factory
    session: LakeSession = await asyncio.to_thread(factory.acquire)
    try:
        yield session
    finally:
        session.__exit__(None, None, None)


def get_lake_settings(request: Request) -> LakeSettings:
    """Return the process-scoped LakeSettings from app state.

    Populated once in lifespan; no per-request lifecycle.
    """
    return request.app.state.lake_settings  # type: ignore[no-any-return]


async def get_optional_lake_session(request: Request) -> AsyncIterator[LakeSession | None]:
    """Yield a DuckDB session if lake_session_factory is configured; else yield None.

    Used by routes that need a lake session only for the iceberg backend path.
    Allows the same route handler to work in test environments where the lake
    infrastructure is not initialised (pg backend only).
    """
    if not hasattr(request.app.state, 'lake_session_factory'):
        yield None
        return
    factory = request.app.state.lake_session_factory
    session: LakeSession = await asyncio.to_thread(factory.acquire)
    try:
        yield session
    finally:
        session.__exit__(None, None, None)
