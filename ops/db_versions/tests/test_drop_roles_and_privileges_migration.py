# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Verifies that ent_roles and ent_privileges tables do not exist after alembic upgrade head.

The tables are dropped by migration ``e4b7c82d9a14`` (Phase 12 Step 1).

Strategy
--------
* A scratch database ``aurelion_migration_test_roles`` is created fresh at the start
  of the test session and destroyed at the end.
* ``alembic upgrade head`` is run as a subprocess against that scratch DB so that
  every migration in the chain (including ``e4b7c82d9a14``) executes for real.
* The test then queries ``information_schema.tables`` to confirm that ``ent_roles``
  and ``ent_privileges`` do not exist.
* The downgrade test loads the migration module directly and asserts that calling
  ``downgrade()`` raises ``NotImplementedError`` — no DB connection needed for that.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KERNEL_ROOT = Path(__file__).resolve().parents[3]  # aurelion-kernel/

_SCRATCH_DB = 'aurelion_migration_test_roles'


def _base_url() -> str:
    """Return DATABASE_URL from env (resolved via .env if needed)."""
    # conftest.py already loaded dotenv; fall back just in case
    url = os.environ.get('DATABASE_URL')
    if not url:
        from dotenv import load_dotenv

        load_dotenv(_KERNEL_ROOT / '.env')
        url = os.environ.get('DATABASE_URL', '')
    return url


def _scratch_url() -> str:
    """Build asyncpg URL pointing at the scratch database."""
    parsed = urlparse(_base_url())
    return urlunparse(parsed._replace(path='/' + _SCRATCH_DB))


def _admin_dsn() -> str:
    """psycopg-style DSN for the *postgres* maintenance DB (for CREATE/DROP DATABASE).

    asyncpg.connect() accepts a DSN string or keyword args; we'll pass keyword args
    directly in the fixtures below.
    """
    parsed = urlparse(_base_url())
    # strip driver prefix so asyncpg can parse it
    host = parsed.hostname or '127.0.0.1'
    port = parsed.port or 5432
    user = parsed.username or 'postgres'
    password = parsed.password or ''
    return host, port, user, password


# ---------------------------------------------------------------------------
# Session-scoped fixture: create scratch DB → run alembic → yield → drop
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope='module')
async def scratch_db_url():
    """Create scratch DB, run ``alembic upgrade head``, yield the async URL, then drop."""
    host, port, user, password = _admin_dsn()

    # 1. Create scratch DB (connect to maintenance DB 'postgres')
    conn = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database='postgres',
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
        await conn.execute(f'CREATE DATABASE "{_SCRATCH_DB}"')
    finally:
        await conn.close()

    # 2. Run alembic upgrade head against the scratch DB
    env = {**os.environ, 'DATABASE_URL': _scratch_url()}
    result = subprocess.run(
        ['uv', 'run', 'alembic', 'upgrade', 'head'],
        cwd=str(_KERNEL_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f'alembic upgrade head failed (rc={result.returncode}):\nstdout: {result.stdout}\nstderr: {result.stderr}'
    )

    yield _scratch_url()

    # 3. Drop scratch DB
    conn = await asyncpg.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database='postgres',
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"')
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ent_roles_table_does_not_exist(scratch_db_url: str) -> None:
    """ent_roles must not exist after alembic upgrade head (migration e4b7c82d9a14)."""
    engine = create_async_engine(scratch_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = await conn.scalar(
                sa.text(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'ent_roles'"
                )
            )
        assert row is None, (
            "Table 'ent_roles' exists in public schema after alembic upgrade head — "
            'migration e4b7c82d9a14 should have dropped it.'
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_ent_privileges_table_does_not_exist(scratch_db_url: str) -> None:
    """ent_privileges must not exist after alembic upgrade head (migration e4b7c82d9a14)."""
    engine = create_async_engine(scratch_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = await conn.scalar(
                sa.text(
                    'SELECT 1 FROM information_schema.tables '
                    "WHERE table_schema = 'public' AND table_name = 'ent_privileges'"
                )
            )
        assert row is None, (
            "Table 'ent_privileges' exists in public schema after alembic upgrade head — "
            'migration e4b7c82d9a14 should have dropped it.'
        )
    finally:
        await engine.dispose()


def test_downgrade_raises_not_implemented() -> None:
    """Downgrade must raise NotImplementedError — the migration is intentionally irreversible."""
    migration_file = _KERNEL_ROOT / 'ops/db_versions/2026_04_23_0000_drop_roles_and_privileges.py'
    spec = importlib.util.spec_from_file_location('_migration_drop_roles_privileges', migration_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['_migration_drop_roles_privileges'] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]

    with pytest.raises(NotImplementedError, match='irreversible'):
        module.downgrade()
