# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Request-less lake dependency factory.

Provides module-level accessors for lake deps (catalog, session, settings) that
can be called from engine action handlers running outside a FastAPI request
context (e.g. platform_executor_node).

Usage in executor node ``_run()``::

    from src.platform.lake.factory import set_process_lake_deps
    set_process_lake_deps(catalog=..., session_factory=..., settings=...)

Action handlers then call::

    from src.platform.lake.factory import get_process_lake_catalog, ...
    catalog = get_process_lake_catalog()

These helpers raise ``RuntimeError`` if accessed before
``set_process_lake_deps`` has been called, which surfaces as a hard startup
failure rather than a silent misconfiguration.

Library-module discipline: no ``get_settings()``, no ``load_dotenv()``,
no ``register_default_providers()`` at import time.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from src.platform.lake.config import LakeSettings
    from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory

# ---------------------------------------------------------------------------
# Module-level process state
# ---------------------------------------------------------------------------

_process_catalog: Catalog | None = None
_process_lake_session_factory: LakeSessionFactory | None = None
_process_lake_settings: LakeSettings | None = None


def set_process_lake_deps(
    *,
    catalog: Catalog,
    session_factory: LakeSessionFactory,
    settings: LakeSettings,
) -> None:
    """Register process-scoped lake deps.  Must be called once at process start."""
    global _process_catalog, _process_lake_session_factory, _process_lake_settings
    _process_catalog = catalog
    _process_lake_session_factory = session_factory
    _process_lake_settings = settings


def get_process_lake_catalog() -> Catalog:
    """Return the process-scoped Iceberg catalog.

    Raises:
        RuntimeError: if ``set_process_lake_deps`` has not been called.
    """
    if _process_catalog is None:
        raise RuntimeError(
            'Process lake catalog not initialised. Call set_process_lake_deps() before invoking lake-backed actions.'
        )
    return _process_catalog


async def get_process_lake_session() -> LakeSession:
    """Acquire a DuckDB session from the process-scoped pool.

    Acquisition is offloaded to a worker thread to avoid blocking the event loop
    (mirrors the FastAPI ``get_lake_session`` dep pattern).

    Raises:
        RuntimeError: if ``set_process_lake_deps`` has not been called.
    """
    if _process_lake_session_factory is None:
        raise RuntimeError(
            'Process lake session factory not initialised. '
            'Call set_process_lake_deps() before invoking lake-backed actions.'
        )
    factory = _process_lake_session_factory
    return await asyncio.to_thread(factory.acquire)


def get_process_lake_settings() -> LakeSettings:
    """Return the process-scoped LakeSettings.

    Raises:
        RuntimeError: if ``set_process_lake_deps`` has not been called.
    """
    if _process_lake_settings is None:
        raise RuntimeError(
            'Process lake settings not initialised. Call set_process_lake_deps() before invoking lake-backed actions.'
        )
    return _process_lake_settings


def reset_process_lake_deps_for_tests() -> None:
    """Reset all process-scoped lake deps to ``None``.

    Call this in test teardown to prevent state leaking between tests.
    Using this helper instead of directly nulling the private globals means
    that renames of those globals cause an *import error* here rather than
    silently leaking state.
    """
    global _process_catalog, _process_lake_session_factory, _process_lake_settings
    _process_catalog = None
    _process_lake_session_factory = None
    _process_lake_settings = None
