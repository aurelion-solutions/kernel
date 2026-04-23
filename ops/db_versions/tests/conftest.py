# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared fixtures for ops/db_versions/tests.

Mirrors the minimal subset of src/conftest.py needed for migration tests:
``engine`` and ``session_factory``. The ``app`` and ``client`` fixtures are
not needed here — migration tests only interact with the DB directly.
"""

import os
from pathlib import Path
import sys
from urllib.parse import urlparse, urlunparse

# Ensure the kernel root (parent of src/) is on sys.path.
# When pytest collects from ops/db_versions/tests/, the importlib mode does not
# add the project root automatically. We need src.* to be importable.
_kernel_root = str(Path(__file__).parent.parent.parent.resolve())
if _kernel_root not in sys.path:
    sys.path.insert(0, _kernel_root)

# These side-effect imports register SQLAlchemy models so Base.metadata is complete.
import importlib as _importlib  # noqa: E402

_importlib.import_module('src.capabilities.effective_access.models')
_importlib.import_module('src.platform.logs.models')

from dotenv import load_dotenv  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

load_dotenv()

# Import Base after path fix
from src.core.db.base import Base  # noqa: E402

DATABASE_URL = os.getenv('DATABASE_URL')
_parsed = urlparse(DATABASE_URL)
_db_name = _parsed.path.lstrip('/')
_test_db = _db_name.rsplit('_', 1)[0] + '_test' if '_' in _db_name else _db_name + '_test'
TEST_DATABASE_URL = urlunparse(_parsed._replace(path='/' + _test_db))


@pytest_asyncio.fixture
async def engine():
    _engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield _engine
    finally:
        async with _engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await _engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
