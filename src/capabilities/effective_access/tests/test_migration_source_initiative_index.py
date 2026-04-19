# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Verifies that ``ix_effective_grants_source_initiative_id`` exists after schema creation.

The index is supplied by both the Alembic migration ``d1a4f7b9c3e5`` and by
``EffectiveGrant.__table_args__`` in ``models.py``, so the test passes against
either the migrated database or the ``Base.metadata.create_all`` test fixture.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa


@pytest.mark.asyncio
async def test_source_initiative_index_exists_after_migration(session_factory) -> None:
    """Assert pg_indexes contains ix_effective_grants_source_initiative_id.

    NOTE: The index is present after either the Alembic migration ``d1a4f7b9c3e5``
    or the ``Base.metadata.create_all`` test fixture (both produce it via
    ``EffectiveGrant.__table_args__``).  Postgres 17 automatically propagates
    parent-table indexes to all child partitions; asserting on the parent table
    name is sufficient.
    """
    async with session_factory() as session:
        result = await session.execute(
            sa.text(
                'SELECT 1 FROM pg_indexes '
                "WHERE indexname = 'ix_effective_grants_source_initiative_id' "
                "  AND tablename = 'effective_grants'"
            )
        )
        row = result.scalar_one_or_none()
        assert row == 1, (
            "Index 'ix_effective_grants_source_initiative_id' not found in pg_indexes "
            "for table 'effective_grants'. Expected it to be produced by "
            'EffectiveGrant.__table_args__ in models.py.'
        )
