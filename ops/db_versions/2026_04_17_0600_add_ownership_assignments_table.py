"""add ownership_assignments table

Revision ID: f6a8b9c0d2e3
Revises: e5f7a8b9c0d2
Create Date: 2026-04-17 06:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'f6a8b9c0d2e3'
down_revision: str | None = 'e5f7a8b9c0d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OWNERSHIP_KIND_VALUES = ('primary', 'secondary', 'technical')


def upgrade() -> None:
    """Upgrade schema."""
    # Create PG enum via idempotent DO-block (same pattern as initiative migration)
    values_sql = ', '.join(f"'{v}'" for v in _OWNERSHIP_KIND_VALUES)
    op.execute(
        f"DO $$ BEGIN CREATE TYPE ownership_kind AS ENUM ({values_sql}); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )

    op.create_table(
        'ownership_assignments',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('resource_id', sa.UUID(), nullable=True),
        sa.Column('account_id', sa.UUID(), nullable=True),
        sa.Column(
            'kind',
            postgresql.ENUM(*_OWNERSHIP_KIND_VALUES, name='ownership_kind', create_type=False),
            nullable=False,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.ForeignKeyConstraint(
            ['subject_id'],
            ['subjects.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['resource_id'],
            ['resources.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['account_id'],
            ['ent_accounts.id'],
            ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            "(resource_id IS NULL) != (account_id IS NULL)",
            name='chk_ownership_assignment_xor_target',
        ),
    )

    # UNIQUE NULLS NOT DISTINCT must be emitted via raw SQL (Alembic does not support the kwarg)
    # Mirrors 2026_04_17_0500_access_facts_unique_nulls_not_distinct.py precedent
    op.execute(
        'ALTER TABLE ownership_assignments ADD CONSTRAINT '
        'uq_ownership_assignments_subject_resource_kind '
        'UNIQUE NULLS NOT DISTINCT (subject_id, resource_id, kind)'
    )
    op.execute(
        'ALTER TABLE ownership_assignments ADD CONSTRAINT '
        'uq_ownership_assignments_subject_account_kind '
        'UNIQUE NULLS NOT DISTINCT (subject_id, account_id, kind)'
    )

    op.create_index(
        'ix_ownership_assignments_subject_id', 'ownership_assignments', ['subject_id']
    )
    op.create_index(
        'ix_ownership_assignments_resource_id', 'ownership_assignments', ['resource_id']
    )
    op.create_index(
        'ix_ownership_assignments_account_id', 'ownership_assignments', ['account_id']
    )
    op.create_index(
        'ix_ownership_assignments_kind', 'ownership_assignments', ['kind']
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_ownership_assignments_kind', table_name='ownership_assignments')
    op.drop_index('ix_ownership_assignments_account_id', table_name='ownership_assignments')
    op.drop_index('ix_ownership_assignments_resource_id', table_name='ownership_assignments')
    op.drop_index('ix_ownership_assignments_subject_id', table_name='ownership_assignments')
    op.execute(
        'ALTER TABLE ownership_assignments DROP CONSTRAINT '
        'uq_ownership_assignments_subject_account_kind'
    )
    op.execute(
        'ALTER TABLE ownership_assignments DROP CONSTRAINT '
        'uq_ownership_assignments_subject_resource_kind'
    )
    op.drop_table('ownership_assignments')
    op.execute("DROP TYPE IF EXISTS ownership_kind")
