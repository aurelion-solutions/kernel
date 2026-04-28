# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Alembic migration tests for Phase 15 Step 14 (lake_migration_runs)."""

from __future__ import annotations

import os
from urllib.parse import urlparse, urlunparse

from alembic import command as alembic_command
from alembic.config import Config
from dotenv import load_dotenv
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL', '')
_parsed = urlparse(DATABASE_URL)
_db_name = _parsed.path.lstrip('/')
_test_db = _db_name.rsplit('_', 1)[0] + '_test' if '_' in _db_name else _db_name + '_test'
TEST_DATABASE_URL = urlunparse(_parsed._replace(path='/' + _test_db))
# Alembic env.py drives an async engine (asyncpg), so feed it the asyncpg URL.
ALEMBIC_TEST_URL = TEST_DATABASE_URL


def _alembic_cfg() -> Config:
    import pathlib  # noqa: PLC0415

    kernel_root = pathlib.Path(__file__).parent.parent.parent.parent.parent
    cfg = Config(str(kernel_root / 'alembic.ini'))
    cfg.set_main_option('sqlalchemy.url', ALEMBIC_TEST_URL)
    return cfg


STEP_14_REVISION = 'g1h2i3j4k5l6'
STEP_10_REVISION = 'f4a5b6c7d8e9'  # prev revision


@pytest.mark.asyncio
async def test_upgrade_creates_table_and_enums() -> None:
    """Upgrade to Step 14 creates lake_migration_runs + 2 enum types + partial unique index."""
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        # Run upgrade up to this revision.
        cfg = _alembic_cfg()
        import asyncio  # noqa: PLC0415

        await asyncio.to_thread(alembic_command.upgrade, cfg, STEP_14_REVISION)

        async with engine.connect() as conn:
            # Check table exists.
            result = await conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_name = 'lake_migration_runs'")
            )
            assert result.scalar_one_or_none() == 'lake_migration_runs'

            # Check enums exist.
            result = await conn.execute(
                text(
                    'SELECT typname FROM pg_type WHERE typname IN '
                    "('lake_migration_dataset', 'lake_migration_status') "
                    'ORDER BY typname'
                )
            )
            types_ = [row[0] for row in result.all()]
            assert 'lake_migration_dataset' in types_
            assert 'lake_migration_status' in types_

            # Check partial unique index exists.
            result = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE indexname = 'uq_reconciliation_delta_items_pg_migration'")
            )
            assert result.scalar_one_or_none() is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_downgrade_drops_table_and_enums() -> None:
    """Downgrade from Step 14 drops lake_migration_runs, enums, and partial index."""
    cfg = _alembic_cfg()
    import asyncio  # noqa: PLC0415

    # Ensure we're at step 14 first.
    await asyncio.to_thread(alembic_command.upgrade, cfg, STEP_14_REVISION)
    # Downgrade to previous revision.
    await asyncio.to_thread(alembic_command.downgrade, cfg, STEP_10_REVISION)

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_name = 'lake_migration_runs'")
            )
            assert result.scalar_one_or_none() is None

            result = await conn.execute(
                text("SELECT typname FROM pg_type WHERE typname IN ('lake_migration_dataset', 'lake_migration_status')")
            )
            assert result.fetchall() == []
    finally:
        await engine.dispose()
