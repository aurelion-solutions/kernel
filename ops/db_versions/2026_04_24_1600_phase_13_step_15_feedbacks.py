"""Phase 13 Step 15 — Feedback slice: add feedbacks table + feedback_kind enum.

Adds:
  - PG enum ``feedback_kind`` with values: accepted_risk, false_positive,
    needs_mapping_fix, needs_rule_fix, needs_mitigation
  - table ``feedbacks`` with:
      - BigInteger PK (autoincrement)
      - nullable FKs: rule_id → sod_rules, capability_mapping_id → capability_mappings,
        finding_id → findings, subject_id → subjects (all ON DELETE RESTRICT)
      - kind (feedback_kind enum, NOT NULL)
      - message (Text, NOT NULL)
      - payload (JSONB, nullable)
      - created_at (timestamptz, server_default now())
      - created_by (String(255), nullable)
      - CHECK: (rule_id IS NOT NULL) OR (capability_mapping_id IS NOT NULL) OR (finding_id IS NOT NULL)
  - indexes: ix_feedbacks_kind_created_at, ix_feedbacks_rule_id,
             ix_feedbacks_capability_mapping_id, ix_feedbacks_finding_id

Downgrade drops the table and enum strictly in reverse order.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = 'd4e5f6a7b8ca'
down_revision = 'c3d4e5f6a7b9'
branch_labels = None
depends_on = None

_FEEDBACK_KIND_ENUM = postgresql.ENUM(
    'accepted_risk',
    'false_positive',
    'needs_mapping_fix',
    'needs_rule_fix',
    'needs_mitigation',
    name='feedback_kind',
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _FEEDBACK_KIND_ENUM.create(bind, checkfirst=False)

    op.create_table(
        'feedbacks',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('rule_id', sa.BigInteger(), nullable=True),
        sa.Column('capability_mapping_id', sa.BigInteger(), nullable=True),
        sa.Column('finding_id', sa.BigInteger(), nullable=True),
        sa.Column('subject_id', UUID(as_uuid=True), nullable=True),
        sa.Column(
            'kind',
            postgresql.ENUM(
                'accepted_risk',
                'false_positive',
                'needs_mapping_fix',
                'needs_rule_fix',
                'needs_mitigation',
                name='feedback_kind',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('payload', JSONB(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('created_by', sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(
            ['capability_mapping_id'],
            ['capability_mappings.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['finding_id'],
            ['findings.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rule_id'],
            ['sod_rules.id'],
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['subject_id'],
            ['subjects.id'],
            ondelete='RESTRICT',
        ),
        sa.CheckConstraint(
            '(rule_id IS NOT NULL) OR (capability_mapping_id IS NOT NULL) OR (finding_id IS NOT NULL)',
            name='ck_feedbacks_target_required',
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_index(
        'ix_feedbacks_kind_created_at',
        'feedbacks',
        ['kind', sa.text('created_at DESC')],
    )
    op.create_index('ix_feedbacks_rule_id', 'feedbacks', ['rule_id'])
    op.create_index(
        'ix_feedbacks_capability_mapping_id', 'feedbacks', ['capability_mapping_id']
    )
    op.create_index('ix_feedbacks_finding_id', 'feedbacks', ['finding_id'])


def downgrade() -> None:
    op.drop_index('ix_feedbacks_finding_id', table_name='feedbacks')
    op.drop_index('ix_feedbacks_capability_mapping_id', table_name='feedbacks')
    op.drop_index('ix_feedbacks_rule_id', table_name='feedbacks')
    op.drop_index('ix_feedbacks_kind_created_at', table_name='feedbacks')
    op.drop_table('feedbacks')
    _FEEDBACK_KIND_ENUM.drop(op.get_bind(), checkfirst=False)
