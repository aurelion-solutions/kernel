"""add access_facts table

Revision ID: aa1bb2cc3dd4
Revises: f8369314b4e5
Create Date: 2026-04-17 02:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'aa1bb2cc3dd4'
down_revision: str | None = 'f8369314b4e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Create PG enum types (idempotent via DO blocks)
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE action AS ENUM ('read', 'write', 'execute', 'approve', 'administer'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE access_fact_effect AS ENUM ('allow', 'deny'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$"
    )

    op.create_table(
        'access_facts',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('account_id', sa.UUID(), nullable=True),
        sa.Column('resource_id', sa.UUID(), nullable=False),
        sa.Column(
            'action',
            postgresql.ENUM('read', 'write', 'execute', 'approve', 'administer', name='action', create_type=False),
            nullable=False,
        ),
        sa.Column(
            'effect',
            postgresql.ENUM('allow', 'deny', name='access_fact_effect', create_type=False),
            nullable=False,
        ),
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
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['account_id'], ['ent_accounts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='RESTRICT'),
        sa.UniqueConstraint(
            'subject_id',
            'account_id',
            'resource_id',
            'action',
            'effect',
            name='uq_access_facts_natural_key',
        ),
    )

    op.create_index('ix_access_facts_subject_id', 'access_facts', ['subject_id'])
    op.create_index('ix_access_facts_resource_id', 'access_facts', ['resource_id'])
    op.create_index('ix_access_facts_account_id', 'access_facts', ['account_id'])
    op.create_index('ix_access_facts_action', 'access_facts', ['action'])
    op.create_index('ix_access_facts_valid_window', 'access_facts', ['valid_from', 'valid_until'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_access_facts_valid_window', table_name='access_facts')
    op.drop_index('ix_access_facts_action', table_name='access_facts')
    op.drop_index('ix_access_facts_account_id', table_name='access_facts')
    op.drop_index('ix_access_facts_resource_id', table_name='access_facts')
    op.drop_index('ix_access_facts_subject_id', table_name='access_facts')
    op.drop_table('access_facts')

    op.execute("DROP TYPE IF EXISTS access_fact_effect")
    op.execute("DROP TYPE IF EXISTS action")
