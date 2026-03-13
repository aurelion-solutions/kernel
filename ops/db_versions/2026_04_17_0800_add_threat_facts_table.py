"""add threat_facts table

Revision ID: b8c2d4e6f8a9
Revises: a7b9c1d3e5f7
Create Date: 2026-04-17 08:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'b8c2d4e6f8a9'
down_revision: str | None = 'a7b9c1d3e5f7'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'threat_facts',
        sa.Column('id', sa.UUID(), primary_key=True),
        sa.Column('subject_id', sa.UUID(), nullable=False),
        sa.Column('account_id', sa.UUID(), nullable=True),
        sa.Column('risk_score', sa.Float(), nullable=False),
        sa.Column(
            'active_indicators',
            postgresql.ARRAY(sa.String(255)),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'failed_auth_count',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
        sa.Column(
            'observed_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
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
            ['subject_id'],
            ['subjects.id'],
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['account_id'],
            ['ent_accounts.id'],
            ondelete='SET NULL',
        ),
        sa.CheckConstraint(
            'risk_score >= 0.0 AND risk_score <= 1.0',
            name='chk_threat_facts_risk_score_range',
        ),
        sa.CheckConstraint(
            'failed_auth_count >= 0',
            name='chk_threat_facts_failed_auth_count_nonneg',
        ),
        sa.UniqueConstraint('subject_id', name='uq_threat_facts_subject_id'),
    )

    op.create_index('ix_threat_facts_account_id', 'threat_facts', ['account_id'])
    op.create_index('ix_threat_facts_risk_score', 'threat_facts', ['risk_score'])
    op.create_index('ix_threat_facts_observed_at', 'threat_facts', ['observed_at'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_threat_facts_observed_at', table_name='threat_facts')
    op.drop_index('ix_threat_facts_risk_score', table_name='threat_facts')
    op.drop_index('ix_threat_facts_account_id', table_name='threat_facts')
    op.drop_table('threat_facts')
