"""Phase 15 Step 10 — SyncApplyRun + SyncApplyResult ORM and Alembic migration.

Creates:
  - PG enum ``sync_apply_run_status``
      (running, completed, failed, partially_applied)
  - PG enum ``sync_apply_run_mode``
      (auto_apply, manual_apply, selected_items, dry_run)
  - PG enum ``sync_apply_result_status``
      (applied, failed, skipped)
  - Table ``sync_apply_runs``
  - Table ``sync_apply_results``

Both tables are greenfield CREATE TABLE — no pre-existing schema exists.

Downgrade notes:
  downgrade only safe before any sync_apply runs are written.
  Dropping ``sync_apply_results`` first (FK dependency order), then
  ``sync_apply_runs``.  Enum types are dropped after the tables to ensure no
  column still references them when DROP TYPE is executed.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'f4a5b6c7d8e9'
down_revision = 'e3f4a5b6c7d8'
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Enum type helpers — create_type=False so Alembic does not attempt DDL
# implicitly; we manage CREATE/DROP explicitly below.
# ---------------------------------------------------------------------------

_run_status = postgresql.ENUM(
    'running',
    'completed',
    'failed',
    'partially_applied',
    name='sync_apply_run_status',
    create_type=False,
)

_run_mode = postgresql.ENUM(
    'auto_apply',
    'manual_apply',
    'selected_items',
    'dry_run',
    name='sync_apply_run_mode',
    create_type=False,
)

_result_status = postgresql.ENUM(
    'applied',
    'failed',
    'skipped',
    name='sync_apply_result_status',
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create the three enum types before any table references them.
    _run_status.create(bind, checkfirst=False)
    _run_mode.create(bind, checkfirst=False)
    _result_status.create(bind, checkfirst=False)

    # 2. sync_apply_runs (must exist before sync_apply_results FK)
    op.create_table(
        'sync_apply_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reconciliation_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            'status',
            postgresql.ENUM(
                'running',
                'completed',
                'failed',
                'partially_applied',
                name='sync_apply_run_status',
                create_type=False,
            ),
            nullable=False,
            server_default='running',
        ),
        sa.Column(
            'mode',
            postgresql.ENUM(
                'auto_apply',
                'manual_apply',
                'selected_items',
                'dry_run',
                name='sync_apply_run_mode',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('requested_by', sa.Text(), nullable=True),
        sa.Column('applied_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('failed_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_sync_apply_runs'),
        sa.ForeignKeyConstraint(
            ['reconciliation_run_id'],
            ['reconciliation_runs.id'],
            ondelete='RESTRICT',
            name='fk_sync_apply_runs_reconciliation_run_id',
        ),
    )
    op.create_index(
        'ix_sync_apply_runs_reconciliation_run_id',
        'sync_apply_runs',
        ['reconciliation_run_id'],
    )
    op.create_index(
        'ix_sync_apply_runs_status',
        'sync_apply_runs',
        ['status'],
    )

    # 3. sync_apply_results
    op.create_table(
        'sync_apply_results',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('sync_apply_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('delta_item_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            'status',
            postgresql.ENUM(
                'applied',
                'failed',
                'skipped',
                name='sync_apply_result_status',
                create_type=False,
            ),
            nullable=False,
        ),
        # Soft lake reference — intentionally no ForeignKeyConstraint
        sa.Column('fact_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('snapshot_id', sa.BigInteger(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint('id', name='pk_sync_apply_results'),
        sa.ForeignKeyConstraint(
            ['sync_apply_run_id'],
            ['sync_apply_runs.id'],
            ondelete='CASCADE',
            name='fk_sync_apply_results_sync_apply_run_id',
        ),
        sa.ForeignKeyConstraint(
            ['delta_item_id'],
            ['reconciliation_delta_items.id'],
            ondelete='RESTRICT',
            name='fk_sync_apply_results_delta_item_id',
        ),
    )
    # Composite index: supports per-run result pagination and crash-recovery scans.
    op.create_index(
        'ix_sync_apply_results_run_status',
        'sync_apply_results',
        ['sync_apply_run_id', 'status'],
    )
    # Single-column index on delta_item_id for crash-recovery in Step 11.
    op.create_index(
        'ix_sync_apply_results_delta_item_id',
        'sync_apply_results',
        ['delta_item_id'],
    )


def downgrade() -> None:
    """Reverse all changes from upgrade().

    downgrade only safe before any sync_apply runs are written.
    """
    bind = op.get_bind()

    # 1. Drop child table first (FK dependency order)
    op.drop_index('ix_sync_apply_results_delta_item_id', table_name='sync_apply_results')
    op.drop_index('ix_sync_apply_results_run_status', table_name='sync_apply_results')
    op.drop_table('sync_apply_results')

    # 2. Drop parent table
    op.drop_index('ix_sync_apply_runs_status', table_name='sync_apply_runs')
    op.drop_index('ix_sync_apply_runs_reconciliation_run_id', table_name='sync_apply_runs')
    op.drop_table('sync_apply_runs')

    # 3. Drop the three enum types owned by this step
    _result_status.drop(bind, checkfirst=False)
    _run_mode.drop(bind, checkfirst=False)
    _run_status.drop(bind, checkfirst=False)
