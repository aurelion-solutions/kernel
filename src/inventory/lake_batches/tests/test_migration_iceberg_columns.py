# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Verifies Phase 15 Step 4 schema state for the lake_batches table.

The test fixture runs ``Base.metadata.create_all`` which reflects the current
ORM model state (post-Step-4), so these assertions validate that both the model
and the migration agree on the final schema.

Assertions:
  1. New columns ``iceberg_namespace``, ``iceberg_table``, ``snapshot_id`` exist and are nullable.
  2. ``storage_provider`` and ``storage_key`` are nullable.
  3. Partial unique index ``uq_lake_batches_storage_provider_storage_key_active`` exists and
     its ``indexdef`` contains ``WHERE`` (confirming it is partial).
  4. Legacy constraint ``uq_lake_batches_storage_provider_storage_key`` is absent from
     ``pg_constraint``.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa


@pytest.mark.asyncio
async def test_iceberg_columns_are_nullable(session_factory) -> None:
    """iceberg_namespace, iceberg_table, snapshot_id exist and are nullable."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'lake_batches'
                  AND column_name IN ('iceberg_namespace', 'iceberg_table', 'snapshot_id')
                ORDER BY column_name
                """
            )
        )
        rows = {r[0]: r[1] for r in result.fetchall()}

    assert set(rows.keys()) == {'iceberg_namespace', 'iceberg_table', 'snapshot_id'}, (
        f'Expected all three Iceberg columns, got: {set(rows.keys())}'
    )
    for col, nullable in rows.items():
        assert nullable == 'YES', f'Column {col!r} expected nullable=YES, got {nullable!r}'


@pytest.mark.asyncio
async def test_storage_coords_are_nullable(session_factory) -> None:
    """storage_provider and storage_key are nullable after Step 4."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                """
                SELECT column_name, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'lake_batches'
                  AND column_name IN ('storage_provider', 'storage_key')
                ORDER BY column_name
                """
            )
        )
        rows = {r[0]: r[1] for r in result.fetchall()}

    assert 'storage_provider' in rows, 'Column storage_provider not found in lake_batches'
    assert 'storage_key' in rows, 'Column storage_key not found in lake_batches'
    assert rows['storage_provider'] == 'YES', (
        f"storage_provider: expected is_nullable='YES', got {rows['storage_provider']!r}"
    )
    assert rows['storage_key'] == 'YES', f"storage_key: expected is_nullable='YES', got {rows['storage_key']!r}"


@pytest.mark.asyncio
async def test_partial_unique_index_exists(session_factory) -> None:
    """Partial unique index uq_lake_batches_storage_provider_storage_key_active exists."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                """
                SELECT indexdef
                FROM pg_indexes
                WHERE tablename = 'lake_batches'
                  AND indexname = 'uq_lake_batches_storage_provider_storage_key_active'
                """
            )
        )
        row = result.scalar_one_or_none()

    assert row is not None, (
        "Index 'uq_lake_batches_storage_provider_storage_key_active' not found in pg_indexes for table 'lake_batches'."
    )
    assert 'WHERE' in row.upper(), f'Index is not partial (no WHERE clause in indexdef): {row!r}'


@pytest.mark.asyncio
async def test_legacy_unique_constraint_absent(session_factory) -> None:
    """Legacy constraint uq_lake_batches_storage_provider_storage_key is NOT present."""
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                """
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'uq_lake_batches_storage_provider_storage_key'
                  AND conrelid = 'lake_batches'::regclass
                """
            )
        )
        row = result.scalar_one_or_none()

    assert row is None, (
        "Legacy unique constraint 'uq_lake_batches_storage_provider_storage_key' still exists on "
        "'lake_batches'. It should have been replaced by the partial unique index."
    )
