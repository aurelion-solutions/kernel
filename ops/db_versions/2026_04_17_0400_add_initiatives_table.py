"""add initiatives table

Revision ID: cc3dd4ee5ff6
Revises: bb2cc3dd4ee5
Create Date: 2026-04-17 04:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'cc3dd4ee5ff6'
down_revision: str | None = 'bb2cc3dd4ee5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INITIATIVE_TYPE_VALUES = (
    'birthright',
    'requested',
    'delegated',
    'inherited',
    'grace',
    'self_registered',
    'invited',
    'trial',
    'subscription',
)


def upgrade() -> None:
    """Upgrade schema."""
    values_sql = ', '.join(f"'{v}'" for v in _INITIATIVE_TYPE_VALUES)
    op.execute(
        f"DO $$ BEGIN CREATE TYPE initiative_type AS ENUM ({values_sql}); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )

    op.create_table(
        'initiatives',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('access_fact_id', sa.UUID(), nullable=False),
        sa.Column(
            'type',
            postgresql.ENUM(*_INITIATIVE_TYPE_VALUES, name='initiative_type', create_type=False),
            nullable=False,
        ),
        sa.Column('origin', sa.String(1024), nullable=False),
        sa.Column(
            'valid_from',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.ForeignKeyConstraint(
            ['access_fact_id'],
            ['access_facts.id'],
            ondelete='CASCADE',
        ),
    )

    op.create_index('ix_initiatives_access_fact_id', 'initiatives', ['access_fact_id'])
    op.create_index('ix_initiatives_type', 'initiatives', ['type'])
    op.create_index(
        'ix_initiatives_valid_window',
        'initiatives',
        ['valid_from', 'valid_until'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_initiatives_valid_window', table_name='initiatives')
    op.drop_index('ix_initiatives_type', table_name='initiatives')
    op.drop_index('ix_initiatives_access_fact_id', table_name='initiatives')
    op.drop_table('initiatives')
    op.execute("DROP TYPE IF EXISTS initiative_type")
