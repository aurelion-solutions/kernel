# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.core.db.session import get_session_factory as _get_session_factory


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with _get_session_factory()() as session:
        yield session


def get_session_factory() -> async_sessionmaker:
    """Return the global session factory.  Override in tests to use the test engine."""
    return _get_session_factory()
