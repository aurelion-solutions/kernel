"""add account to inventory_reconcile_entity_type

Revision ID: a3f7c2d891e0
Revises: b1be83106f6e
Create Date: 2026-05-12 22:47:00.000000

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a3f7c2d891e0'
down_revision: Union[str, Sequence[str], None] = 'b1be83106f6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostgreSQL ALTER TYPE ... ADD VALUE is non-transactional.
    # Use IF NOT EXISTS for idempotency (PG 9.6+).
    op.execute("ALTER TYPE inventory_reconcile_entity_type ADD VALUE IF NOT EXISTS 'account'")


def downgrade() -> None:
    # PostgreSQL does not support removing enum values.
    # A full recreate would be required; for now this is a no-op.
    pass
