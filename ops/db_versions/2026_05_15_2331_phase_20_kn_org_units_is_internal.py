"""Phase 20 K-N — add org_units.is_internal column with per-tree consistency trigger.

Revision ID: c9e4f1a20b37
Revises: a3f7c2d891e0
Create Date: 2026-05-15 23:31:00.000000

Adds a boolean ``is_internal`` column to ``org_units`` (default ``true``)
and a PL/pgSQL ``BEFORE INSERT OR UPDATE`` trigger that enforces per-tree
consistency: every node in a connected tree must share the same
``is_internal`` value.

The trigger SQL is the single source of truth in
``src.inventory.org_units._trigger_sql`` so the migration and the test
fixtures can never drift.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from src.inventory.org_units._trigger_sql import (
    TRIGGER_CREATE_SQL,
    TRIGGER_DROP_IF_EXISTS,
    TRIGGER_FUNC_SQL,
)

revision: str = 'c9e4f1a20b37'
down_revision: Union[str, Sequence[str], None] = 'a3f7c2d891e0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'org_units',
        sa.Column(
            'is_internal',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )
    op.execute(TRIGGER_FUNC_SQL)
    op.execute(TRIGGER_DROP_IF_EXISTS)
    op.execute(TRIGGER_CREATE_SQL)


def downgrade() -> None:
    op.execute(TRIGGER_DROP_IF_EXISTS)
    op.execute('DROP FUNCTION IF EXISTS org_units_assert_is_internal_consistency()')
    op.drop_column('org_units', 'is_internal')
