# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lazy async database engine and session factory.

Both ``get_engine()`` and ``get_session_factory()`` are ``lru_cache``-wrapped
so that the engine is created at most once per process.  They read
``get_settings().postgres.dsn`` lazily — the settings singleton is not
evaluated at import time.

Tests must call ``get_engine.cache_clear()`` and
``get_session_factory.cache_clear()`` between test cases because
pytest-asyncio creates a fresh event loop per test and ``AsyncEngine`` is
bound to the loop it was created on.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from src.core.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    """Return the cached async engine.

    Creates a new ``AsyncEngine`` on first call using the DSN from
    ``get_settings().postgres.dsn``.  Subsequent calls return the same
    instance.
    """
    dsn = get_settings().postgres.dsn
    return create_async_engine(dsn, pool_pre_ping=True)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the cached async session factory.

    Bound to ``get_engine()``.  Cache is shared for the process lifetime.
    """
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
