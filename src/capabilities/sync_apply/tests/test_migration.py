# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Migration round-trip test for Phase 15 Step 10.

Exercises upgrade() and downgrade() from the migration module directly,
without invoking the Alembic CLI or env.py.

Round-trip sequence:
  1. upgrade()   → assert both tables and three enum types exist.
  2. downgrade() → assert both tables and all three enum types are gone.
  3. upgrade()   → idempotent; same assertions as step 1.

Alembic's ``op`` functions are synchronous and require a synchronous DBAPI
connection.  The project only ships ``asyncpg`` (no psycopg2/psycopg).
We therefore use SQLAlchemy's ``conn.run_sync`` bridge: each
``async with engine.begin() as conn`` block calls
``await conn.run_sync(_do_upgrade)`` which passes a *synchronous* raw
connection to ``alembic.runtime.migration.MigrationContext``.
"""

from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import Any
from urllib.parse import urlparse, urlunparse

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from dotenv import load_dotenv
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

load_dotenv()

_MIGRATION_MODULE = 'ops.db_versions.2026_04_27_0400_phase_15_step_10_sync_apply_model'

_TABLES = ('sync_apply_runs', 'sync_apply_results')
_ENUMS = (
    'sync_apply_run_status',
    'sync_apply_run_mode',
    'sync_apply_result_status',
)


def _make_async_test_url() -> str:
    """Derive the test database async URL from DATABASE_URL."""
    raw = os.environ['DATABASE_URL']
    parsed = urlparse(raw)
    db_name = parsed.path.lstrip('/')
    test_db = db_name.rsplit('_', 1)[0] + '_test' if '_' in db_name else db_name + '_test'
    return urlunparse(parsed._replace(path='/' + test_db))


# ---------------------------------------------------------------------------
# Sync helpers called via conn.run_sync inside async context
# ---------------------------------------------------------------------------


def _sync_upgrade(sync_conn: Any, mod: ModuleType) -> None:
    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        mod.upgrade()


def _sync_downgrade(sync_conn: Any, mod: ModuleType) -> None:
    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        mod.downgrade()


def _sync_drop_sync_apply_schema(sync_conn: Any) -> None:
    """Drop sync_apply tables and enum types if they exist (idempotent cleanup)."""
    # Drop child before parent (FK order)
    sync_conn.execute(sa.text('DROP TABLE IF EXISTS sync_apply_results CASCADE'))
    sync_conn.execute(sa.text('DROP TABLE IF EXISTS sync_apply_runs CASCADE'))
    for enum_name in _ENUMS:
        sync_conn.execute(sa.text(f'DROP TYPE IF EXISTS {enum_name} CASCADE'))


def _sync_table_exists(sync_conn: Any, table_name: str) -> bool:
    row = sync_conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"),
        {'t': table_name},
    ).scalar_one_or_none()
    return row == 1


def _sync_enum_exists(sync_conn: Any, enum_name: str) -> bool:
    row = sync_conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :e AND typtype = 'e'"),
        {'e': enum_name},
    ).scalar_one_or_none()
    return row == 1


def _sync_assert_present(sync_conn: Any) -> None:
    for table in _TABLES:
        assert _sync_table_exists(sync_conn, table), f'Table {table!r} not found after upgrade()'
    for enum in _ENUMS:
        assert _sync_enum_exists(sync_conn, enum), f'Enum {enum!r} not found after upgrade()'


def _sync_assert_absent(sync_conn: Any) -> None:
    for table in _TABLES:
        assert not _sync_table_exists(sync_conn, table), f'Table {table!r} still exists after downgrade()'
    for enum in _ENUMS:
        assert not _sync_enum_exists(sync_conn, enum), f'Enum {enum!r} still exists after downgrade()'


def _sync_column_names(sync_conn: Any, table_name: str) -> set[str]:
    result = sync_conn.execute(
        sa.text("SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t"),
        {'t': table_name},
    )
    return {row[0] for row in result.fetchall()}


def _sync_enum_labels(sync_conn: Any) -> dict[str, list[str]]:
    result = sync_conn.execute(
        sa.text(
            'SELECT t.typname, e.enumlabel '
            'FROM pg_enum e '
            'JOIN pg_type t ON t.oid = e.enumtypid '
            'WHERE t.typname IN ('
            "  'sync_apply_run_status',"
            "  'sync_apply_run_mode',"
            "  'sync_apply_result_status'"
            ') ORDER BY t.typname, e.enumsortorder'
        )
    )
    by_type: dict[str, list[str]] = {}
    for type_name, label in result.fetchall():
        by_type.setdefault(type_name, []).append(label)
    return by_type


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope='module')
async def migration_engine() -> AsyncEngine:  # type: ignore[misc]
    """Async engine scoped to this module; sets up prerequisite tables.

    Prerequisite tables (reconciliation_runs, reconciliation_delta_items, etc.)
    are created once for the module.  Each test manages its own sync_apply
    tables via the ``clean_sync_apply`` per-function fixture.
    """
    import src.capabilities.access_analysis.capabilities.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.capability_grants.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.capability_mappings.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.capability_scope_keys.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.feedbacks.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.findings.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.mitigation_controls.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.mitigations.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.scan_runs.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.sod_rule_conditions.models  # noqa: F401, PLC0415
    import src.capabilities.access_analysis.sod_rules.models  # noqa: F401, PLC0415
    import src.capabilities.effective_access.models  # noqa: F401, PLC0415
    import src.capabilities.reconciliation.models  # noqa: F401, PLC0415
    import src.capabilities.sync_apply.models  # noqa: F401, PLC0415
    from src.capabilities.sync_apply.models import SyncApplyResult, SyncApplyRun  # noqa: PLC0415
    from src.core.db.base import Base  # noqa: PLC0415
    import src.inventory.actions.models  # noqa: F401, PLC0415
    import src.inventory.lake_batches.models  # noqa: F401, PLC0415
    import src.platform.llm.models  # noqa: F401, PLC0415
    import src.platform.logs.models  # noqa: F401, PLC0415

    engine = create_async_engine(_make_async_test_url(), poolclass=NullPool)

    sync_apply_tables = {SyncApplyRun.__table__, SyncApplyResult.__table__}
    tables_to_create = [t for t in Base.metadata.sorted_tables if t not in sync_apply_tables]

    async with engine.begin() as conn:
        # Full teardown first (handles dirty state from prior runs)
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.run_sync(_sync_drop_sync_apply_schema)
        # Recreate prerequisite types and tables
        await conn.execute(sa.text("CREATE TYPE llm_provider AS ENUM ('llama_cpp', 'openai', 'ollama')"))
        await conn.run_sync(Base.metadata.create_all, tables=tables_to_create)

    yield engine  # type: ignore[misc]

    # Module teardown
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.run_sync(_sync_drop_sync_apply_schema)

    await engine.dispose()


@pytest_asyncio.fixture
async def clean_sync_apply(migration_engine: AsyncEngine) -> None:  # type: ignore[misc]
    """Drop sync_apply tables/enums before and after each test."""
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_drop_sync_apply_schema)
    yield  # type: ignore[misc]
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_drop_sync_apply_schema)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_round_trip(migration_engine: AsyncEngine, clean_sync_apply: None) -> None:
    """upgrade → assert present → downgrade → assert absent → upgrade again (idempotent)."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)

    # Step 1: upgrade
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_sync_assert_present)

    # Step 2: downgrade
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_downgrade, mod)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_sync_assert_absent)

    # Step 3: upgrade again — idempotent
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_sync_assert_present)


@pytest.mark.asyncio
async def test_sync_apply_runs_columns(migration_engine: AsyncEngine, clean_sync_apply: None) -> None:
    """All expected columns are present on sync_apply_runs after upgrade."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)
    expected = {
        'id',
        'reconciliation_run_id',
        'status',
        'mode',
        'started_at',
        'finished_at',
        'created_at',
        'requested_by',
        'applied_count',
        'failed_count',
        'error',
    }

    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    actual: set[str] = set()

    def _get_cols(sync_conn: Any) -> None:
        nonlocal actual
        actual = _sync_column_names(sync_conn, 'sync_apply_runs')

    async with migration_engine.connect() as conn:
        await conn.run_sync(_get_cols)

    assert expected <= actual, f'Missing columns in sync_apply_runs: {expected - actual}'


@pytest.mark.asyncio
async def test_sync_apply_results_columns(migration_engine: AsyncEngine, clean_sync_apply: None) -> None:
    """All expected columns are present on sync_apply_results after upgrade."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)
    expected = {
        'id',
        'sync_apply_run_id',
        'delta_item_id',
        'status',
        'fact_id',
        'snapshot_id',
        'error',
        'created_at',
    }

    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    actual: set[str] = set()

    def _get_cols(sync_conn: Any) -> None:
        nonlocal actual
        actual = _sync_column_names(sync_conn, 'sync_apply_results')

    async with migration_engine.connect() as conn:
        await conn.run_sync(_get_cols)

    assert expected <= actual, f'Missing columns in sync_apply_results: {expected - actual}'


@pytest.mark.asyncio
async def test_enum_labels(migration_engine: AsyncEngine, clean_sync_apply: None) -> None:
    """Enum types carry the expected labels after upgrade."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)

    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    by_type: dict[str, list[str]] = {}

    def _get_labels(sync_conn: Any) -> None:
        nonlocal by_type
        by_type = _sync_enum_labels(sync_conn)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_get_labels)

    assert set(by_type.get('sync_apply_run_status', [])) == {
        'running',
        'completed',
        'failed',
        'partially_applied',
    }, f'sync_apply_run_status labels mismatch: {by_type.get("sync_apply_run_status")}'

    assert set(by_type.get('sync_apply_run_mode', [])) == {
        'auto_apply',
        'manual_apply',
        'selected_items',
        'dry_run',
    }, f'sync_apply_run_mode labels mismatch: {by_type.get("sync_apply_run_mode")}'

    assert set(by_type.get('sync_apply_result_status', [])) == {
        'applied',
        'failed',
        'skipped',
    }, f'sync_apply_result_status labels mismatch: {by_type.get("sync_apply_result_status")}'
