# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Migration integration tests for 2026_04_24_0000_add_ref_actions.

Exercises the upgrade() and downgrade() functions from the migration module
directly, without invoking the Alembic CLI or env.py (which unconditionally
overwrites sqlalchemy.url with settings.database_url and would hit the
production database).

The approach:
- Table structure is verified via information_schema after create_all (conftest).
- Seed rows are verified by calling the migration's upgrade() directly via
  alembic.operations.Operations against a fresh table inside an isolated
  transaction that is rolled back after each test.
- downgrade() is verified by creating a table via upgrade() and then dropping
  it via downgrade() — both inside a single rolled-back savepoint block.

Service test: not applicable — no service in this sub-step (Step 2b).
API test: not applicable — no routes in this sub-step (Step 2c).
CLI test: not applicable — no CLI in this sub-step (Step 2d).
"""

from __future__ import annotations

import importlib

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
import pytest
import sqlalchemy as sa

_EXPECTED_SLUGS = ['read', 'write', 'execute', 'approve', 'admin', 'use', 'own']
_EXPECTED_DESCRIPTIONS = {
    'read': 'Observe a resource without modifying it.',
    'write': 'Modify a resource.',
    'execute': 'Trigger an operation on a resource.',
    'approve': 'Approve a request or transaction.',
    'admin': 'Administer configuration of a resource.',
    'use': 'Consume a resource as a functional user.',
    'own': 'Ownership-level control of a resource.',
}

_MIGRATION_MODULE = 'ops.db_versions.2026_04_24_0000_add_ref_actions'


def _load_migration():
    """Import the migration module by dotted name."""
    return importlib.import_module(_MIGRATION_MODULE)


@pytest.mark.asyncio
async def test_ref_actions_table_exists(session_factory) -> None:
    """Table is present after Base.metadata.create_all (conftest fixture)."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'ref_actions'"
            )
        )
        row = result.scalar_one_or_none()
    assert row == 1, "Table 'ref_actions' not found — expected create_all to produce it."


@pytest.mark.asyncio
async def test_upgrade_seeds_seven_rows_with_exact_slugs(session_factory) -> None:
    """upgrade() bulk-inserts the seven canonical action slugs in order."""
    migration = _load_migration()

    async with session_factory() as session:
        # Run upgrade() inside a nested savepoint so we can roll back the seed
        # without affecting the surrounding transaction.
        async with session.begin_nested():
            # upgrade() uses op.bulk_insert — wire it to the live connection.
            conn = await session.connection()

            def _run(sync_conn):
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.upgrade()

            await conn.run_sync(_run)

            result = await session.execute(sa.text('SELECT slug FROM ref_actions ORDER BY id'))
            slugs = [r[0] for r in result.fetchall()]

        # savepoint rolls back here — table is empty again after the block

    assert slugs == _EXPECTED_SLUGS, f'Expected slugs {_EXPECTED_SLUGS!r}, got {slugs!r}'


@pytest.mark.asyncio
async def test_upgrade_seeds_descriptions(session_factory) -> None:
    """upgrade() seeds correct description text for every slug."""
    migration = _load_migration()

    async with session_factory() as session:
        async with session.begin_nested():
            conn = await session.connection()

            def _run(sync_conn):
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.upgrade()

            await conn.run_sync(_run)

            result = await session.execute(sa.text('SELECT slug, description FROM ref_actions ORDER BY id'))
            rows = {r[0]: r[1] for r in result.fetchall()}

    assert rows == _EXPECTED_DESCRIPTIONS, (
        f'Seed descriptions mismatch.\nExpected: {_EXPECTED_DESCRIPTIONS!r}\nGot: {rows!r}'
    )


@pytest.mark.asyncio
async def test_downgrade_drops_ref_actions_table(session_factory) -> None:
    """downgrade() removes the ref_actions table.

    This test is self-contained: it seeds the table via upgrade(), asserts the
    table exists, calls downgrade(), then asserts the table is gone — all
    inside a single rolled-back savepoint so sibling tests are unaffected.
    """
    migration = _load_migration()

    async with session_factory() as session:
        async with session.begin_nested():
            conn = await session.connection()

            def _run_upgrade(sync_conn):
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.upgrade()

            await conn.run_sync(_run_upgrade)

            # Confirm table exists after upgrade
            result = await session.execute(
                sa.text(
                    'SELECT 1 FROM information_schema.tables '
                    "WHERE table_schema = 'public' AND table_name = 'ref_actions'"
                )
            )
            assert result.scalar_one_or_none() == 1, "Table 'ref_actions' should exist after upgrade()."

            def _run_downgrade(sync_conn):
                ctx = MigrationContext.configure(sync_conn)
                with Operations.context(ctx):
                    migration.downgrade()

            await conn.run_sync(_run_downgrade)

            result = await session.execute(
                sa.text(
                    'SELECT 1 FROM information_schema.tables '
                    "WHERE table_schema = 'public' AND table_name = 'ref_actions'"
                )
            )
            after_drop = result.scalar_one_or_none()

        # savepoint rolls back — ref_actions is restored to its create_all state

    assert after_drop is None, "Table 'ref_actions' still exists after downgrade() — expected it to be dropped."
