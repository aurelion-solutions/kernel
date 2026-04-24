"""Phase 13 Step 8 — MitigationControl catalog slice.

Creates:
  - mitigation_control_type Postgres enum
  - mitigation_controls table (with unique constraint and indexes)
  - Seeds five standard catalog entries (one per enum value) via ON CONFLICT DO NOTHING

Downgrade reverses strictly:
  1. drop mitigation_controls table (indexes drop implicitly)
  2. drop mitigation_control_type enum
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'a1b2c3d4e5f7'
down_revision = 'f0a1b2c3d4e5'
branch_labels = None
depends_on = None

_SEED_ROWS = [
    {
        'code': 'QUARTERLY_ATTESTATION',
        'name': 'Quarterly access attestation',
        'type': 'attestation',
        'description': (
            'Periodic attestation that the access is still required and approved '
            'by a designated reviewer.'
        ),
    },
    {
        'code': 'DUAL_APPROVAL',
        'name': 'Dual approval on sensitive operations',
        'type': 'dual_approval',
        'description': 'Operation requires two distinct approvers before being executed.',
    },
    {
        'code': 'SIEM_ALERTING',
        'name': 'SIEM alerting on usage',
        'type': 'logging_alerting',
        'description': (
            'All uses of this access are forwarded to the SIEM with alerting on '
            'policy violations.'
        ),
    },
    {
        'code': 'COMPENSATING_PROCESS',
        'name': 'Compensating business process',
        'type': 'compensating_process',
        'description': (
            'A documented business process compensates for the access risk '
            '(e.g. independent monthly reconciliation).'
        ),
    },
    {
        'code': 'OTHER',
        'name': 'Other',
        'type': 'other',
        'description': (
            'Catch-all for controls that do not fit the four primary categories. '
            'To be split out as the catalog matures.'
        ),
    },
]


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create mitigation_control_type enum
    mitigation_control_type = postgresql.ENUM(
        'attestation',
        'dual_approval',
        'logging_alerting',
        'compensating_process',
        'other',
        name='mitigation_control_type',
        create_type=False,
    )
    mitigation_control_type.create(bind, checkfirst=False)

    # 2. Create mitigation_controls table
    op.create_table(
        'mitigation_controls',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'type',
            postgresql.ENUM(
                'attestation',
                'dual_approval',
                'logging_alerting',
                'compensating_process',
                'other',
                name='mitigation_control_type',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_mitigation_controls'),
        sa.UniqueConstraint('code', name='uq_mitigation_controls_code'),
    )
    op.create_index('ix_mitigation_controls_is_active', 'mitigation_controls', ['is_active'])
    op.create_index('ix_mitigation_controls_type', 'mitigation_controls', ['type'])

    # 3. Seed catalog — idempotent via ON CONFLICT (code) DO NOTHING
    bind.execute(
        sa.text(
            'INSERT INTO mitigation_controls (code, name, type, description) '
            'VALUES (:code, :name, CAST(:type AS mitigation_control_type), :description) '
            'ON CONFLICT (code) DO NOTHING'
        ),
        _SEED_ROWS,
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Drop indexes and table
    op.drop_index('ix_mitigation_controls_type', table_name='mitigation_controls')
    op.drop_index('ix_mitigation_controls_is_active', table_name='mitigation_controls')
    op.drop_table('mitigation_controls')

    # Drop the enum type owned by this step only
    mitigation_control_type = postgresql.ENUM(
        'attestation',
        'dual_approval',
        'logging_alerting',
        'compensating_process',
        'other',
        name='mitigation_control_type',
        create_type=False,
    )
    mitigation_control_type.drop(bind, checkfirst=False)
