"""add access_usage_facts table

Revision ID: a7b9c1d3e5f7
Revises: f6a8b9c0d2e3
Create Date: 2026-04-17 07:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a7b9c1d3e5f7'
down_revision: str | None = 'f6a8b9c0d2e3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'access_usage_facts',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('access_fact_id', sa.UUID(), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.Column('usage_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('window_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('window_to', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.ForeignKeyConstraint(
            ['access_fact_id'],
            ['access_facts.id'],
            ondelete='CASCADE',
        ),
        sa.CheckConstraint('usage_count >= 0', name='chk_access_usage_facts_usage_count_nonneg'),
        sa.CheckConstraint(
            'window_to IS NULL OR window_to > window_from',
            name='chk_access_usage_facts_window_ordering',
        ),
    )

    # UNIQUE NULLS NOT DISTINCT must be emitted via raw SQL — Alembic ignores postgresql_nulls_not_distinct kwarg
    # in create_unique_constraint. This is the same precedent as ownership_assignments migration.
    op.execute(
        'ALTER TABLE access_usage_facts ADD CONSTRAINT '
        'uq_access_usage_facts_fact_window '
        'UNIQUE NULLS NOT DISTINCT (access_fact_id, window_from, window_to)'
    )

    op.create_index('ix_access_usage_facts_access_fact_id', 'access_usage_facts', ['access_fact_id'])
    op.create_index('ix_access_usage_facts_last_seen', 'access_usage_facts', ['last_seen'])
    op.create_index('ix_access_usage_facts_window', 'access_usage_facts', ['window_from', 'window_to'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_access_usage_facts_window', table_name='access_usage_facts')
    op.drop_index('ix_access_usage_facts_last_seen', table_name='access_usage_facts')
    op.drop_index('ix_access_usage_facts_access_fact_id', table_name='access_usage_facts')
    op.execute(
        'ALTER TABLE access_usage_facts DROP CONSTRAINT uq_access_usage_facts_fact_window'
    )
    op.drop_table('access_usage_facts')
