"""Phase 13 Step 7 — ScanRun + Finding slices.

Creates:
  - scan_run_status Postgres enum
  - scan_run_trigger Postgres enum
  - finding_kind Postgres enum
  - finding_status Postgres enum
  - scan_runs table (with CHECK constraints and indexes)
  - findings table (with CHECK constraints, UNIQUE constraint, and indexes)
    Reuses existing sod_severity enum via create_type=False.

No seed data.

Downgrade reverses strictly:
  1. drop findings indexes
  2. drop findings table
  3. drop scan_runs indexes
  4. drop scan_runs table
  5. drop finding_status enum
  6. drop finding_kind enum
  7. drop scan_run_trigger enum
  8. drop scan_run_status enum
  (sod_severity is NOT dropped — owned by Step 6)
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'f0a1b2c3d4e5'
down_revision = 'e8f9a0b1c2d3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create scan_run_status enum
    scan_run_status = postgresql.ENUM(
        'pending', 'running', 'completed', 'failed',
        name='scan_run_status',
        create_type=False,
    )
    scan_run_status.create(bind, checkfirst=False)

    # 2. Create scan_run_trigger enum
    scan_run_trigger = postgresql.ENUM(
        'manual', 'api', 'schedule',
        name='scan_run_trigger',
        create_type=False,
    )
    scan_run_trigger.create(bind, checkfirst=False)

    # 3. Create finding_kind enum
    finding_kind = postgresql.ENUM(
        'sod', 'orphan_access', 'terminated_access', 'unused_access',
        name='finding_kind',
        create_type=False,
    )
    finding_kind.create(bind, checkfirst=False)

    # 4. Create finding_status enum
    finding_status = postgresql.ENUM(
        'open', 'acknowledged', 'resolved', 'mitigated',
        name='finding_status',
        create_type=False,
    )
    finding_status.create(bind, checkfirst=False)

    # 5. Create scan_runs table
    op.create_table(
        'scan_runs',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column(
            'status',
            postgresql.ENUM(
                'pending', 'running', 'completed', 'failed',
                name='scan_run_status',
                create_type=False,
            ),
            nullable=False,
            server_default='pending',
        ),
        sa.Column(
            'triggered_by',
            postgresql.ENUM(
                'manual', 'api', 'schedule',
                name='scan_run_trigger',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('scope_subject_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('scope_application_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('findings_total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('findings_by_severity', postgresql.JSONB(), nullable=False, server_default='{}'),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_scan_runs'),
        sa.ForeignKeyConstraint(
            ['scope_subject_id'],
            ['subjects.id'],
            name='scan_runs_scope_subject_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scope_application_id'],
            ['applications.id'],
            name='scan_runs_scope_application_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.CheckConstraint(
            "completed_at IS NULL OR status IN ('completed', 'failed')",
            name='ck_scan_runs_completed_at_terminal',
        ),
        sa.CheckConstraint(
            "started_at IS NOT NULL OR status = 'pending'",
            name='ck_scan_runs_started_at_not_pending',
        ),
        sa.CheckConstraint(
            'findings_total >= 0',
            name='ck_scan_runs_findings_total_nonneg',
        ),
    )
    op.create_index('ix_scan_runs_status', 'scan_runs', ['status'])
    op.create_index(
        'ix_scan_runs_created_at_desc',
        'scan_runs',
        [sa.text('created_at DESC')],
    )
    op.create_index('ix_scan_runs_scope_subject_id', 'scan_runs', ['scope_subject_id'])
    op.create_index('ix_scan_runs_scope_application_id', 'scan_runs', ['scope_application_id'])

    # 6. Create findings table
    # Note: sod_severity is reused from Step 6 — create_type=False, no .create() call.
    op.create_table(
        'findings',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('scan_run_id', sa.BigInteger(), nullable=False),
        sa.Column(
            'kind',
            postgresql.ENUM(
                'sod', 'orphan_access', 'terminated_access', 'unused_access',
                name='finding_kind',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('subject_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('rule_id', sa.BigInteger(), nullable=True),
        sa.Column('scope_key_id', sa.BigInteger(), nullable=True),
        sa.Column('scope_value', sa.String(length=255), nullable=True),
        sa.Column(
            'severity',
            postgresql.ENUM(
                'critical', 'high', 'medium', 'low', 'informational',
                name='sod_severity',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            'status',
            postgresql.ENUM(
                'open', 'acknowledged', 'resolved', 'mitigated',
                name='finding_status',
                create_type=False,
            ),
            nullable=False,
            server_default='open',
        ),
        sa.Column('matched_capability_grant_ids', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('matched_effective_grant_ids', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('matched_access_fact_ids', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('evidence_hash', sa.String(length=64), nullable=False),
        sa.Column('active_mitigation_id', sa.BigInteger(), nullable=True),
        sa.Column('proposed_mitigation_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'detected_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('evaluated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('status_changed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('status_reason', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_findings'),
        sa.ForeignKeyConstraint(
            ['scan_run_id'],
            ['scan_runs.id'],
            name='findings_scan_run_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['subject_id'],
            ['subjects.id'],
            name='findings_subject_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['account_id'],
            ['ent_accounts.id'],
            name='findings_account_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['rule_id'],
            ['sod_rules.id'],
            name='findings_rule_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scope_key_id'],
            ['capability_scope_keys.id'],
            name='findings_scope_key_id_fkey',
            ondelete='RESTRICT',
        ),
        # Uniqueness
        sa.UniqueConstraint(
            'kind',
            'subject_id',
            'account_id',
            'rule_id',
            'scope_key_id',
            'scope_value',
            'evidence_hash',
            name='uq_findings_evidence',
        ),
        # CHECK constraints
        sa.CheckConstraint(
            "(kind = 'sod') = (rule_id IS NOT NULL)",
            name='ck_findings_rule_id_for_sod',
        ),
        sa.CheckConstraint(
            'subject_id IS NOT NULL OR account_id IS NOT NULL',
            name='ck_findings_subject_or_account',
        ),
        sa.CheckConstraint(
            "kind <> 'orphan_access' OR subject_id IS NULL",
            name='ck_findings_orphan_no_subject',
        ),
    )
    op.create_index('ix_findings_subject_status', 'findings', ['subject_id', 'status'])
    op.create_index('ix_findings_rule_status', 'findings', ['rule_id', 'status'])
    op.create_index(
        'ix_findings_kind_status_detected',
        'findings',
        ['kind', 'status', sa.text('detected_at DESC')],
    )
    op.create_index('ix_findings_severity_status', 'findings', ['severity', 'status'])
    op.create_index('ix_findings_active_mitigation_id', 'findings', ['active_mitigation_id'])
    op.create_index('ix_findings_proposed_mitigation_id', 'findings', ['proposed_mitigation_id'])
    op.create_index('ix_findings_scan_run_id', 'findings', ['scan_run_id'])


def downgrade() -> None:
    bind = op.get_bind()

    # Drop findings indexes first
    op.drop_index('ix_findings_scan_run_id', table_name='findings')
    op.drop_index('ix_findings_proposed_mitigation_id', table_name='findings')
    op.drop_index('ix_findings_active_mitigation_id', table_name='findings')
    op.drop_index('ix_findings_severity_status', table_name='findings')
    op.drop_index('ix_findings_kind_status_detected', table_name='findings')
    op.drop_index('ix_findings_rule_status', table_name='findings')
    op.drop_index('ix_findings_subject_status', table_name='findings')
    op.drop_table('findings')

    # Drop scan_runs indexes then table
    op.drop_index('ix_scan_runs_scope_application_id', table_name='scan_runs')
    op.drop_index('ix_scan_runs_scope_subject_id', table_name='scan_runs')
    op.drop_index('ix_scan_runs_created_at_desc', table_name='scan_runs')
    op.drop_index('ix_scan_runs_status', table_name='scan_runs')
    op.drop_table('scan_runs')

    # Drop owned enums (sod_severity is NOT dropped — owned by Step 6)
    finding_status = postgresql.ENUM(
        'open', 'acknowledged', 'resolved', 'mitigated',
        name='finding_status',
        create_type=False,
    )
    finding_status.drop(bind, checkfirst=False)

    finding_kind = postgresql.ENUM(
        'sod', 'orphan_access', 'terminated_access', 'unused_access',
        name='finding_kind',
        create_type=False,
    )
    finding_kind.drop(bind, checkfirst=False)

    scan_run_trigger = postgresql.ENUM(
        'manual', 'api', 'schedule',
        name='scan_run_trigger',
        create_type=False,
    )
    scan_run_trigger.drop(bind, checkfirst=False)

    scan_run_status = postgresql.ENUM(
        'pending', 'running', 'completed', 'failed',
        name='scan_run_status',
        create_type=False,
    )
    scan_run_status.drop(bind, checkfirst=False)
