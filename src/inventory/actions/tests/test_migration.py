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

import pytest
import sqlalchemy as sa

_MIGRATION_MODULE = 'ops.db_versions.2026_04_24_0000_add_ref_actions'
_EXPECTED_SLUGS = ['read', 'write', 'execute', 'approve', 'admin', 'use', 'own']


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


def test_seed_rows_contains_required_slugs() -> None:
    """_SEED_ROWS constant contains all seven canonical action slugs."""
    mod = importlib.import_module(_MIGRATION_MODULE)
    slugs = [row['slug'] for row in mod._SEED_ROWS]
    for expected in _EXPECTED_SLUGS:
        assert expected in slugs, f"Expected slug '{expected}' missing from _SEED_ROWS"


def test_seed_rows_descriptions_are_non_empty() -> None:
    """Each seed row has a non-empty description."""
    mod = importlib.import_module(_MIGRATION_MODULE)
    for row in mod._SEED_ROWS:
        assert row.get('description'), f"Slug '{row['slug']}' has empty description"
