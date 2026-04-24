"""Phase 13 Step 2 — CapabilityScopeKey vocabulary slice (schema only).

Default vocabulary is seeded by the next migration
``2026_04_24_0800_seed_capability_scope_keys.py``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a4b5c6d7e8f9'
down_revision = 'f3a0b1c2d3e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'capability_scope_keys',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(64), nullable=False),
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
    op.create_unique_constraint('uq_capability_scope_keys_code', 'capability_scope_keys', ['code'])
    op.create_index('ix_capability_scope_keys_is_active', 'capability_scope_keys', ['is_active'])


def downgrade() -> None:
    op.drop_index('ix_capability_scope_keys_is_active', table_name='capability_scope_keys')
    op.drop_constraint('uq_capability_scope_keys_code', 'capability_scope_keys', type_='unique')
    op.drop_table('capability_scope_keys')
