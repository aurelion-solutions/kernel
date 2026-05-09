# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Migration round-trip test for Phase 15 Step 7.

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

Import smoke: importing the migration module also imports
``src.engines.reconciliation.models``, which validates model structure
at collection time.
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

_MIGRATION_MODULE = 'ops.db_versions.2026_04_27_0300_phase_15_step_07_reconciliation_delta_model'

_TABLES = ('reconciliation_runs', 'reconciliation_delta_items')
_ENUMS = (
    'reconciliation_run_status',
    'reconciliation_delta_operation',
    'reconciliation_delta_item_status',
)


def _make_async_test_url() -> str:
    """Derive the test database async URL from settings/DATABASE_URL."""
    raw: str | None
    try:
        from src.core.config import get_settings  # noqa: PLC0415

        raw = get_settings().postgres.dsn
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        raw = os.getenv('DATABASE_URL')
    if not raw:
        raise RuntimeError('Cannot resolve database URL: no secrets file and DATABASE_URL not set')
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


def _sync_drop_recon_schema(sync_conn: Any) -> None:
    """Drop reconciliation tables and enum types if they exist (idempotent cleanup)."""
    # Drop child before parent (FK order)
    sync_conn.execute(sa.text('DROP TABLE IF EXISTS reconciliation_delta_items CASCADE'))
    sync_conn.execute(sa.text('DROP TABLE IF EXISTS reconciliation_runs CASCADE'))
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
            "  'reconciliation_run_status',"
            "  'reconciliation_delta_operation',"
            "  'reconciliation_delta_item_status'"
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

    Prerequisite tables (applications, lake_batches, etc.) are created once
    for the module.  Each test manages its own reconciliation tables via
    the ``clean_recon`` per-function fixture.
    """
    from src.core.db.base import Base  # noqa: PLC0415
    import src.engines.effective_access.models  # noqa: F401, PLC0415
    import src.engines.reconciliation.models  # noqa: F401, PLC0415
    from src.engines.reconciliation.models import (  # noqa: PLC0415
        ReconciliationDeltaItem,
        ReconciliationRun,
    )
    import src.inventory.access_model.capabilities.models  # noqa: F401, PLC0415
    import src.inventory.access_model.capability_grants.models  # noqa: F401, PLC0415
    import src.inventory.access_model.capability_mappings.models  # noqa: F401, PLC0415
    import src.inventory.access_model.capability_scope_keys.models  # noqa: F401, PLC0415
    import src.inventory.actions.models  # noqa: F401, PLC0415
    import src.inventory.assessment.feedbacks.models  # noqa: F401, PLC0415
    import src.inventory.assessment.findings.models  # noqa: F401, PLC0415
    import src.inventory.assessment.mitigation_controls.models  # noqa: F401, PLC0415
    import src.inventory.assessment.mitigations.models  # noqa: F401, PLC0415
    import src.inventory.assessment.scan_runs.models  # noqa: F401, PLC0415
    import src.inventory.lake_batches.models  # noqa: F401, PLC0415
    import src.inventory.policy.sod_rule_conditions.models  # noqa: F401, PLC0415
    import src.inventory.policy.sod_rules.models  # noqa: F401, PLC0415
    import src.platform.llm.models  # noqa: F401, PLC0415
    import src.platform.logs.models  # noqa: F401, PLC0415

    engine = create_async_engine(_make_async_test_url(), poolclass=NullPool)

    recon_tables = {ReconciliationRun.__table__, ReconciliationDeltaItem.__table__}
    # Exclude tables with FKs into recon tables — they cannot be created until
    # the migration creates reconciliation tables in each test.
    recon_dependents = {
        t for t in Base.metadata.sorted_tables for fk in t.foreign_keys if fk.column.table in recon_tables
    }
    excluded = recon_tables | recon_dependents
    tables_to_create = [t for t in Base.metadata.sorted_tables if t not in excluded]

    async with engine.begin() as conn:
        # Full teardown first (handles dirty state from prior runs).  The
        # session-scoped ``_provision_test_database`` fixture in
        # ``src/conftest.py`` adds two raw shim tables (``access_facts``,
        # ``access_artifacts``) that are NOT in ``Base.metadata`` and carry
        # FKs INTO ORM tables (``subjects``, ``ref_actions``, ``resources``,
        # ``applications``).  ``Base.metadata.drop_all`` cannot remove them
        # and would fail with ``DependentObjectsStillExistError``, so drop
        # them manually first.
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_facts CASCADE'))
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_artifacts CASCADE'))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.execute(sa.text('DROP TYPE IF EXISTS access_fact_effect CASCADE'))
        await conn.run_sync(_sync_drop_recon_schema)
        # Recreate prerequisite types and tables
        await conn.execute(sa.text("CREATE TYPE llm_provider AS ENUM ('llama_cpp', 'openai', 'ollama')"))
        await conn.execute(sa.text("CREATE TYPE access_fact_effect AS ENUM ('allow', 'deny')"))
        await conn.run_sync(Base.metadata.create_all, tables=tables_to_create)

    yield engine  # type: ignore[misc]

    # Module teardown — same shim drop dance, then leave the DB cleaned out.
    # The session-scoped provisioner will repopulate the shim tables at the
    # next session start, but mid-session the unit-test ``engine`` fixture
    # also auto-recovers via ``_ensure_schema_intact`` (see src/conftest.py).
    async with engine.begin() as conn:
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_facts CASCADE'))
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_artifacts CASCADE'))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.execute(sa.text('DROP TYPE IF EXISTS access_fact_effect CASCADE'))
        await conn.run_sync(_sync_drop_recon_schema)

    await engine.dispose()


@pytest_asyncio.fixture
async def clean_recon(migration_engine: AsyncEngine) -> None:  # type: ignore[misc]
    """Drop reconciliation tables/enums before and after each test."""
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_drop_recon_schema)
    yield  # type: ignore[misc]
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_drop_recon_schema)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_round_trip(migration_engine: AsyncEngine, clean_recon: None) -> None:
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
async def test_reconciliation_runs_columns(migration_engine: AsyncEngine, clean_recon: None) -> None:
    """All expected columns are present on reconciliation_runs after upgrade."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)
    expected = {
        'id',
        'application_id',
        'observed_batch_id',
        'observed_snapshot_id',
        'current_snapshot_id',
        'status',
        'created_at',
        'started_at',
        'finished_at',
        'created_count',
        'updated_count',
        'revoked_count',
        'unchanged_count',
        'error',
    }

    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    actual: set[str] = set()

    def _get_cols(sync_conn: Any) -> None:
        nonlocal actual
        actual = _sync_column_names(sync_conn, 'reconciliation_runs')

    async with migration_engine.connect() as conn:
        await conn.run_sync(_get_cols)

    assert expected <= actual, f'Missing columns in reconciliation_runs: {expected - actual}'


@pytest.mark.asyncio
async def test_reconciliation_delta_items_columns(migration_engine: AsyncEngine, clean_recon: None) -> None:
    """All expected columns are present on reconciliation_delta_items after upgrade."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)
    expected = {
        'id',
        'reconciliation_run_id',
        'operation',
        'natural_key_hash',
        'subject_id',
        'account_id',
        'resource_id',
        'action_id',
        'effect',
        'existing_fact_id',
        'source_artifact_id',
        'before_json',
        'after_json',
        'status',
        'reason',
        'created_at',
        'applied_at',
    }

    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    actual: set[str] = set()

    def _get_cols(sync_conn: Any) -> None:
        nonlocal actual
        actual = _sync_column_names(sync_conn, 'reconciliation_delta_items')

    async with migration_engine.connect() as conn:
        await conn.run_sync(_get_cols)

    assert expected <= actual, f'Missing columns in reconciliation_delta_items: {expected - actual}'


@pytest.mark.asyncio
async def test_enum_labels(migration_engine: AsyncEngine, clean_recon: None) -> None:
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

    assert set(by_type.get('reconciliation_run_status', [])) == {
        'running',
        'pending_apply',
        'failed',
        'applied',
        'partially_applied',
        'discarded',
        'dry_run_completed',
    }, f'reconciliation_run_status labels mismatch: {by_type.get("reconciliation_run_status")}'

    assert set(by_type.get('reconciliation_delta_operation', [])) == {
        'create',
        'update',
        'revoke',
        'reactivate',
        'noop',
    }, f'reconciliation_delta_operation labels mismatch: {by_type.get("reconciliation_delta_operation")}'

    assert set(by_type.get('reconciliation_delta_item_status', [])) == {
        'pending',
        'approved',
        'rejected',
        'applied',
        'failed',
        'ignored',
    }, f'reconciliation_delta_item_status labels mismatch: {by_type.get("reconciliation_delta_item_status")}'
