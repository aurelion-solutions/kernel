"""add artifact_bindings table

Revision ID: bb2cc3dd4ee5
Revises: aa1bb2cc3dd4
Create Date: 2026-04-17 03:00:00.000000

"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'bb2cc3dd4ee5'
down_revision: str | None = 'aa1bb2cc3dd4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'artifact_bindings',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('artifact_id', sa.UUID(), nullable=False),
        sa.Column('access_fact_id', sa.UUID(), nullable=True),
        sa.Column('resource_id', sa.UUID(), nullable=True),
        sa.Column('account_id', sa.UUID(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.ForeignKeyConstraint(['artifact_id'], ['access_artifacts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['access_fact_id'], ['access_facts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['resource_id'], ['resources.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['account_id'], ['ent_accounts.id'], ondelete='CASCADE'),
        sa.CheckConstraint(
            "COALESCE(access_fact_id::text, resource_id::text, account_id::text) IS NOT NULL",
            name='chk_artifact_binding_has_target',
        ),
    )

    op.create_index('ix_artifact_bindings_artifact_id', 'artifact_bindings', ['artifact_id'])
    op.create_index('ix_artifact_bindings_access_fact_id', 'artifact_bindings', ['access_fact_id'])
    op.create_index('ix_artifact_bindings_resource_id', 'artifact_bindings', ['resource_id'])
    op.create_index('ix_artifact_bindings_account_id', 'artifact_bindings', ['account_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_artifact_bindings_account_id', table_name='artifact_bindings')
    op.drop_index('ix_artifact_bindings_resource_id', table_name='artifact_bindings')
    op.drop_index('ix_artifact_bindings_access_fact_id', table_name='artifact_bindings')
    op.drop_index('ix_artifact_bindings_artifact_id', table_name='artifact_bindings')
    op.drop_table('artifact_bindings')
