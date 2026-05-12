# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Migration round-trip test for Phase 18 Step 4.

Exercises upgrade() and downgrade() from the migration module directly,
without invoking the Alembic CLI or env.py.

Round-trip sequence:
  1. upgrade()   → assert all three tables, four enum types, and the partial
                   UNIQUE index exist; verify the WHERE clause text.
  2. downgrade() → assert tables, enums, and indexes are all gone.
  3. upgrade()   → idempotent; same assertions as step 1.

Also provides a dedicated test for the partial UNIQUE predicate text to catch
any accidental drift when the migration is edited in the future.

Alembic's ``op`` functions are synchronous and require a synchronous DBAPI
connection.  The project only ships ``asyncpg`` (no psycopg2/psycopg).
We therefore use SQLAlchemy's ``conn.run_sync`` bridge.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
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

_MIGRATION_MODULE = 'ops.db_versions.2026_05_10_0100_phase_18_step_04_orchestrator_tables'

_TABLES = ('pipeline_runs', 'step_runs', 'pipeline_event_waiters')
_ENUMS = (
    'pipeline_event_waiter_status',
    'pipeline_run_status',
    'pipeline_trigger_source',
    'step_run_status',
)
# Substring that must be present in the partial UNIQUE index definition.
_PARTIAL_UNIQUE_WHERE_FRAGMENT = 'retry_of_run_id IS NULL'


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


def _sync_drop_orchestrator_schema(sync_conn: Any) -> None:
    """Drop orchestrator tables and enum types if they exist (idempotent cleanup)."""
    sync_conn.execute(sa.text('DROP TABLE IF EXISTS pipeline_event_waiters CASCADE'))
    sync_conn.execute(sa.text('DROP TABLE IF EXISTS step_runs CASCADE'))
    sync_conn.execute(sa.text('DROP TABLE IF EXISTS pipeline_runs CASCADE'))
    for enum_name in _ENUMS:
        sync_conn.execute(sa.text(f'DROP TYPE IF EXISTS {enum_name} CASCADE'))


def _sync_table_exists(sync_conn: Any, table_name: str) -> bool:
    row = sync_conn.execute(
        sa.text("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t"),
        {'t': table_name},
    ).scalar_one_or_none()
    return bool(row == 1)


def _sync_enum_exists(sync_conn: Any, enum_name: str) -> bool:
    row = sync_conn.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :e AND typtype = 'e'"),
        {'e': enum_name},
    ).scalar_one_or_none()
    return bool(row == 1)


def _sync_index_exists(sync_conn: Any, index_name: str) -> bool:
    row = sync_conn.execute(
        sa.text('SELECT 1 FROM pg_indexes WHERE indexname = :i'),
        {'i': index_name},
    ).scalar_one_or_none()
    return bool(row == 1)


def _sync_partial_unique_indexdef(sync_conn: Any) -> str | None:
    """Return the pg_indexes.indexdef for the partial UNIQUE, or None if absent."""
    row: str | None = sync_conn.execute(
        sa.text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_pipeline_runs_inflight_idempotency'"),
    ).scalar_one_or_none()
    return row


def _sync_assert_present(sync_conn: Any) -> None:
    for table in _TABLES:
        assert _sync_table_exists(sync_conn, table), f'Table {table!r} not found after upgrade()'
    for enum in _ENUMS:
        assert _sync_enum_exists(sync_conn, enum), f'Enum {enum!r} not found after upgrade()'
    assert _sync_index_exists(sync_conn, 'uq_pipeline_runs_inflight_idempotency'), (
        "Partial UNIQUE index 'uq_pipeline_runs_inflight_idempotency' not found after upgrade()"
    )


def _sync_assert_absent(sync_conn: Any) -> None:
    for table in _TABLES:
        assert not _sync_table_exists(sync_conn, table), f'Table {table!r} still exists after downgrade()'
    for enum in _ENUMS:
        assert not _sync_enum_exists(sync_conn, enum), f'Enum {enum!r} still exists after downgrade()'
    assert not _sync_index_exists(sync_conn, 'uq_pipeline_runs_inflight_idempotency'), (
        'Partial UNIQUE index still exists after downgrade()'
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope='module')
async def migration_engine() -> AsyncGenerator[AsyncEngine]:
    """Module-scoped async engine; sets up prerequisite base schema."""
    from src.core.db.base import Base  # noqa: PLC0415
    import src.engines.effective_access.models  # noqa: F401, PLC0415
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
    import src.inventory.policy.sod_rule_conditions.models  # noqa: F401, PLC0415
    import src.inventory.policy.sod_rules.models  # noqa: F401, PLC0415
    import src.platform.llm.models  # noqa: F401, PLC0415
    import src.platform.logs.models  # noqa: F401, PLC0415
    import src.platform.orchestrator.models  # noqa: F401, PLC0415
    from src.platform.orchestrator.models import (  # noqa: PLC0415
        PipelineEventWaiter,
        PipelineRun,
        StepRun,
    )

    engine = create_async_engine(_make_async_test_url(), poolclass=NullPool)

    orchestrator_tables = {PipelineRun.__table__, StepRun.__table__, PipelineEventWaiter.__table__}
    orchestrator_dependents = {
        t for t in Base.metadata.sorted_tables for fk in t.foreign_keys if fk.column.table in orchestrator_tables
    }
    excluded = orchestrator_tables | orchestrator_dependents
    tables_to_create = [t for t in Base.metadata.sorted_tables if t not in excluded]

    async with engine.begin() as conn:
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_facts CASCADE'))
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_artifacts CASCADE'))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.execute(sa.text('DROP TYPE IF EXISTS access_fact_effect CASCADE'))
        await conn.run_sync(_sync_drop_orchestrator_schema)
        await conn.execute(sa.text("CREATE TYPE llm_provider AS ENUM ('llama_cpp', 'openai', 'ollama')"))
        await conn.execute(sa.text("CREATE TYPE access_fact_effect AS ENUM ('allow', 'deny')"))
        await conn.run_sync(Base.metadata.create_all, tables=tables_to_create)

    yield engine  # noqa: PT022 — cleanup after yield is required

    async with engine.begin() as conn:
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_facts CASCADE'))
        await conn.execute(sa.text('DROP TABLE IF EXISTS access_artifacts CASCADE'))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.execute(sa.text('DROP TYPE IF EXISTS access_fact_effect CASCADE'))
        await conn.run_sync(_sync_drop_orchestrator_schema)

    await engine.dispose()


@pytest_asyncio.fixture
async def clean_orchestrator(migration_engine: AsyncEngine) -> AsyncGenerator[None]:
    """Drop orchestrator tables/enums before and after each test."""
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_drop_orchestrator_schema)
    yield  # noqa: PT022 — cleanup after yield is required
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_drop_orchestrator_schema)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_round_trip(migration_engine: AsyncEngine, clean_orchestrator: None) -> None:
    """upgrade → assert present → downgrade → assert absent → upgrade again (idempotent)."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)

    # Step 1: upgrade.
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_sync_assert_present)

    # Step 2: downgrade.
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_downgrade, mod)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_sync_assert_absent)

    # Step 3: upgrade again — idempotent.
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_sync_assert_present)


@pytest.mark.asyncio
async def test_partial_unique_predicate_text_present(migration_engine: AsyncEngine, clean_orchestrator: None) -> None:
    """The partial UNIQUE WHERE clause must reference retry_of_run_id IS NULL.

    This test is a lock on the predicate text.  If the migration is edited and
    the WHERE clause drifts, this test will catch it before a silent-duplicate
    run reaches production.
    """
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)

    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    indexdef: str | None = None

    def _get_indexdef(sync_conn: Any) -> None:
        nonlocal indexdef
        indexdef = _sync_partial_unique_indexdef(sync_conn)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_get_indexdef)

    assert indexdef is not None, 'Partial UNIQUE index not found'
    assert _PARTIAL_UNIQUE_WHERE_FRAGMENT in indexdef, (
        f'Expected WHERE clause fragment {_PARTIAL_UNIQUE_WHERE_FRAGMENT!r} not found in indexdef: {indexdef!r}'
    )
    # Also verify the in-flight status list is encoded.
    assert 'cancelling' in indexdef, f"'cancelling' not in partial UNIQUE WHERE clause: {indexdef!r}"
