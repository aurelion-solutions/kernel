"""Phase 13 Step 9 — Mitigation slice.

Creates:
  - mitigation_status Postgres enum (proposed, active, expired, revoked)
  - mitigations table with all FKs, CHECK constraints, and indexes
    - partial unique index uq_mitigations_active_or_proposed with NULLS NOT DISTINCT (PG17)
  - Deferred FK constraints from findings:
    - findings_active_mitigation_id_fkey → mitigations.id
    - findings_proposed_mitigation_id_fkey → mitigations.id

NULLS NOT DISTINCT on the partial unique index ensures that two unscoped mitigations
(scope_key_id IS NULL, scope_value IS NULL) for the same (rule_id, subject_id) pair
cannot both be active or proposed simultaneously.  PG would treat NULLs as distinct
without this clause, allowing duplicates.  Project runs PG17 (supported since PG15).

Downgrade reverses strictly:
  1. drop findings FK constraints
  2. drop mitigations indexes and table
  3. drop mitigation_status enum
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'b2c3d4e5f6a8'
down_revision = 'a1b2c3d4e5f7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create mitigation_status enum
    mitigation_status = postgresql.ENUM(
        'proposed',
        'active',
        'expired',
        'revoked',
        name='mitigation_status',
        create_type=False,
    )
    mitigation_status.create(bind, checkfirst=False)

    # 2. Create mitigations table
    op.create_table(
        'mitigations',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('rule_id', sa.BigInteger(), nullable=False),
        sa.Column('control_id', sa.BigInteger(), nullable=False),
        sa.Column(
            'subject_id',
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column('scope_key_id', sa.BigInteger(), nullable=True),
        sa.Column('scope_value', sa.String(length=255), nullable=True),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column(
            'status',
            postgresql.ENUM(
                'proposed',
                'active',
                'expired',
                'revoked',
                name='mitigation_status',
                create_type=False,
            ),
            nullable=False,
            server_default='proposed',
        ),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'owner_id',
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_mitigations'),
        sa.ForeignKeyConstraint(['rule_id'], ['sod_rules.id'], ondelete='RESTRICT', name='fk_mitigations_rule_id'),
        sa.ForeignKeyConstraint(['control_id'], ['mitigation_controls.id'], ondelete='RESTRICT', name='fk_mitigations_control_id'),
        sa.ForeignKeyConstraint(['subject_id'], ['subjects.id'], ondelete='RESTRICT', name='fk_mitigations_subject_id'),
        sa.ForeignKeyConstraint(['scope_key_id'], ['capability_scope_keys.id'], ondelete='RESTRICT', name='fk_mitigations_scope_key_id'),
        sa.ForeignKeyConstraint(['owner_id'], ['subjects.id'], ondelete='RESTRICT', name='fk_mitigations_owner_id'),
        sa.CheckConstraint(
            '(scope_key_id IS NULL) = (scope_value IS NULL)',
            name='ck_mitigations_scope_pair',
        ),
        sa.CheckConstraint(
            'valid_until IS NULL OR valid_until > valid_from',
            name='ck_mitigations_valid_window',
        ),
    )

    # Partial unique index with NULLS NOT DISTINCT (PG15+; project runs PG17).
    # Prevents two active/proposed mitigations for the same (rule, subject, scope) tuple,
    # including unscoped mitigations where both scope columns are NULL.
    op.execute(
        sa.text(
            'CREATE UNIQUE INDEX uq_mitigations_active_or_proposed '
            'ON mitigations (rule_id, subject_id, scope_key_id, scope_value) '
            "NULLS NOT DISTINCT "
            "WHERE status IN ('active', 'proposed')"
        )
    )

    # Evaluator primary lookup
    op.create_index(
        'ix_mitigations_subject_rule_status',
        'mitigations',
        ['subject_id', 'rule_id', 'status', 'valid_from', 'valid_until'],
    )
    # Expiry sweep candidate scan
    op.create_index('ix_mitigations_valid_until', 'mitigations', ['valid_until'])
    # Usage inspection when admins inspect a control
    op.create_index('ix_mitigations_control_id', 'mitigations', ['control_id'])
    # Owner dashboards
    op.create_index('ix_mitigations_owner_id', 'mitigations', ['owner_id'])

    # 3. Add deferred FKs from findings (columns were added as plain BigInteger by Step 7)
    op.create_foreign_key(
        'findings_active_mitigation_id_fkey',
        'findings',
        'mitigations',
        ['active_mitigation_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'findings_proposed_mitigation_id_fkey',
        'findings',
        'mitigations',
        ['proposed_mitigation_id'],
        ['id'],
        ondelete='RESTRICT',
    )


def downgrade() -> None:
    bind = op.get_bind()

    # 1. Drop findings FK constraints first (mitigations table must exist to drop them)
    op.drop_constraint('findings_active_mitigation_id_fkey', 'findings', type_='foreignkey')
    op.drop_constraint('findings_proposed_mitigation_id_fkey', 'findings', type_='foreignkey')

    # 2. Drop indexes and table
    op.drop_index('ix_mitigations_owner_id', table_name='mitigations')
    op.drop_index('ix_mitigations_control_id', table_name='mitigations')
    op.drop_index('ix_mitigations_valid_until', table_name='mitigations')
    op.drop_index('ix_mitigations_subject_rule_status', table_name='mitigations')
    op.execute(sa.text('DROP INDEX IF EXISTS uq_mitigations_active_or_proposed'))
    op.drop_table('mitigations')

    # 3. Drop the enum type owned by this step only
    mitigation_status = postgresql.ENUM(
        'proposed',
        'active',
        'expired',
        'revoked',
        name='mitigation_status',
        create_type=False,
    )
    mitigation_status.drop(bind, checkfirst=False)
