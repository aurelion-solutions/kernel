# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Migration round-trip test for Phase 15 Step 15.

Exercises upgrade() and downgrade() from the migration module directly,
without invoking the Alembic CLI or env.py.

Round-trip sequence:
  1. Setup: add the FK constraint manually (simulates pre-Step-15 schema state,
     because models.py has already removed the ForeignKey declaration).
  2. upgrade()   → assert FK absent; UNIQUE constraint + both indexes still present.
  3. downgrade() → assert FK back with CASCADE (confdeltype = 'c').
  4. upgrade()   → leave DB clean (head state for module teardown).

Alembic ``op`` functions are synchronous and require a synchronous DBAPI connection.
The project only ships ``asyncpg`` (no psycopg2/psycopg).
We use SQLAlchemy's ``conn.run_sync`` bridge: each ``async with engine.begin() as conn``
block calls ``await conn.run_sync(_do_something)`` which passes a *synchronous* raw
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

_MIGRATION_MODULE = 'ops.db_versions.2026_04_27_0600_phase_15_step_15_drop_artifact_bindings_fk'


def _make_async_test_url() -> str:
    """Derive the test database async URL from settings/DATABASE_URL."""
    raw: str | None
    try:
        from src.core.config import get_settings  # noqa: PLC0415

        raw = get_settings().postgres.dsn
    except Exception:
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


def _sync_add_fk(sync_conn: Any) -> None:
    """Synthetically add the FK constraint (simulates pre-Step-15 schema state).

    Because models.py no longer declares ForeignKey on artifact_id,
    Base.metadata.create_all() will not create the FK.  We add it manually
    so that upgrade() has something to drop.
    """
    sync_conn.execute(
        sa.text(
            'ALTER TABLE artifact_bindings '
            'ADD CONSTRAINT artifact_bindings_artifact_id_fkey '
            'FOREIGN KEY (artifact_id) REFERENCES access_artifacts(id) '
            'ON DELETE CASCADE'
        )
    )


def _sync_fk_present(sync_conn: Any) -> bool:
    """Return True if a FK from artifact_bindings.artifact_id → access_artifacts exists."""
    row = sync_conn.execute(
        sa.text(
            'SELECT 1 '
            'FROM information_schema.table_constraints tc '
            'JOIN information_schema.key_column_usage kcu '
            '  ON tc.constraint_name = kcu.constraint_name '
            '  AND tc.table_schema = kcu.table_schema '
            "WHERE tc.table_name = 'artifact_bindings' "
            "  AND tc.constraint_type = 'FOREIGN KEY' "
            "  AND kcu.column_name = 'artifact_id'"
        )
    ).scalar_one_or_none()
    return row == 1


def _sync_fk_ondelete(sync_conn: Any) -> str | None:
    """Return the confdeltype char for the FK on artifact_bindings.artifact_id, or None."""
    row = sync_conn.execute(
        sa.text(
            'SELECT c.confdeltype '
            'FROM pg_constraint c '
            "WHERE c.conrelid = 'artifact_bindings'::regclass "
            "  AND c.contype = 'f' "
            '  AND c.conkey = ARRAY[('
            '    SELECT attnum FROM pg_attribute '
            "    WHERE attrelid = 'artifact_bindings'::regclass "
            "      AND attname = 'artifact_id'"
            '  )]::int2[]'
        )
    ).scalar_one_or_none()
    return row


def _sync_unique_present(sync_conn: Any) -> bool:
    """Return True if uq_artifact_bindings_artifact_id_target_type_target_id exists."""
    row = sync_conn.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_artifact_bindings_artifact_id_target_type_target_id'")
    ).scalar_one_or_none()
    return row == 1


def _sync_index_present(sync_conn: Any, index_name: str) -> bool:
    """Return True if the named index exists in pg_indexes."""
    row = sync_conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = 'public' AND indexname = :name"),
        {'name': index_name},
    ).scalar_one_or_none()
    return row == 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope='module')
async def migration_engine() -> AsyncEngine:  # type: ignore[misc]
    """Async engine scoped to this module; sets up the full schema (minus the FK).

    All tables are created via Base.metadata.create_all().  Because models.py has
    already dropped the ForeignKey declaration, the artifact_bindings.artifact_id FK
    will NOT be present after create_all — tests add it manually as needed.
    """
    from src.core.db.base import Base  # noqa: PLC0415
    import src.engines.effective_access.models  # noqa: F401, PLC0415
    import src.engines.lake_migration.models  # noqa: F401, PLC0415
    import src.engines.reconciliation.models  # noqa: F401, PLC0415
    import src.engines.sync_apply.models  # noqa: F401, PLC0415
    import src.inventory.access_model.capabilities.models  # noqa: F401, PLC0415
    import src.inventory.access_model.capability_grants.models  # noqa: F401, PLC0415
    import src.inventory.access_model.capability_mappings.models  # noqa: F401, PLC0415
    import src.inventory.access_model.capability_scope_keys.models  # noqa: F401, PLC0415
    import src.inventory.actions.models  # noqa: F401, PLC0415
    import src.inventory.artifact_bindings.models  # noqa: F401, PLC0415
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

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.execute(sa.text("CREATE TYPE llm_provider AS ENUM ('llama_cpp', 'openai', 'ollama')"))
        await conn.run_sync(Base.metadata.create_all)

    yield engine  # type: ignore[misc]

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))

    await engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason='Phase 15 Step 16 removed access_artifacts from PG (moved to Iceberg). '
    'This Step-15 round-trip test cannot rebuild the FK target — the simulated '
    'pre-Step-15 state is no longer reachable on a head schema. Kept for history.'
)
@pytest.mark.asyncio
async def test_drop_fk_round_trip(migration_engine: AsyncEngine) -> None:
    """FK drop round-trip: setup → upgrade (drop) → downgrade (restore) → upgrade (cleanup)."""
    mod: ModuleType = importlib.import_module(_MIGRATION_MODULE)

    # Step 1: synthetically add the FK to simulate pre-Step-15 state.
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_add_fk)

    # Verify pre-state: FK is present.
    fk_before: list[bool] = []

    def _check_before(sync_conn: Any) -> None:
        fk_before.append(_sync_fk_present(sync_conn))

    async with migration_engine.connect() as conn:
        await conn.run_sync(_check_before)

    assert fk_before[0] is True, 'Pre-condition failed: FK should be present before upgrade'

    # Step 2: apply upgrade (drop FK).
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)

    # Step 3: assert FK absent; UNIQUE + indexes intact.
    results_after_upgrade: dict[str, Any] = {}

    def _check_after_upgrade(sync_conn: Any) -> None:
        results_after_upgrade['fk_present'] = _sync_fk_present(sync_conn)
        results_after_upgrade['unique_present'] = _sync_unique_present(sync_conn)
        results_after_upgrade['ix_artifact_id'] = _sync_index_present(sync_conn, 'ix_artifact_bindings_artifact_id')
        results_after_upgrade['ix_target'] = _sync_index_present(sync_conn, 'ix_artifact_bindings_target')

    async with migration_engine.connect() as conn:
        await conn.run_sync(_check_after_upgrade)

    assert results_after_upgrade['fk_present'] is False, 'FK should be absent after upgrade'
    assert results_after_upgrade['unique_present'] is True, (
        'UNIQUE uq_artifact_bindings_artifact_id_target_type_target_id must survive upgrade'
    )
    assert results_after_upgrade['ix_artifact_id'] is True, (
        'Index ix_artifact_bindings_artifact_id must survive upgrade'
    )
    assert results_after_upgrade['ix_target'] is True, 'Index ix_artifact_bindings_target must survive upgrade'

    # Step 4: downgrade (restore FK).
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_downgrade, mod)

    # Step 5: assert FK back with CASCADE.
    results_after_downgrade: dict[str, Any] = {}

    def _check_after_downgrade(sync_conn: Any) -> None:
        results_after_downgrade['fk_present'] = _sync_fk_present(sync_conn)
        results_after_downgrade['ondelete'] = _sync_fk_ondelete(sync_conn)

    async with migration_engine.connect() as conn:
        await conn.run_sync(_check_after_downgrade)

    assert results_after_downgrade['fk_present'] is True, 'FK should be restored after downgrade'
    assert results_after_downgrade['ondelete'] == 'c', "FK ON DELETE action must be CASCADE ('c') after downgrade"

    # Step 6: re-upgrade to leave DB in clean (head) state.
    async with migration_engine.begin() as conn:
        await conn.run_sync(_sync_upgrade, mod)
