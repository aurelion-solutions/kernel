"""Phase 13 Step 1 — Capability vocabulary slice.

CRUD only. No seed data — the capability catalog is data shipped via separate
seed migrations / fixtures, not by this schema migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f3a0b1c2d3e4'
down_revision = 'e6f7a8b9c0d1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'capabilities',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('slug', sa.String(128), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('created_by', sa.String(255), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_unique_constraint('uq_capabilities_slug', 'capabilities', ['slug'])
    op.create_index('ix_capabilities_is_active', 'capabilities', ['is_active'])


def downgrade() -> None:
    op.drop_index('ix_capabilities_is_active', table_name='capabilities')
    op.drop_constraint('uq_capabilities_slug', 'capabilities', type_='unique')
    op.drop_table('capabilities')
