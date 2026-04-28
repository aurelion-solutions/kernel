"""Phase 15 Step 7 — ReconciliationRun + ReconciliationDeltaItem ORM and Alembic migration.

Creates:
  - PG enum ``reconciliation_run_status``
      (running, pending_apply, failed, applied, partially_applied, discarded, dry_run_completed)
  - PG enum ``reconciliation_delta_operation``
      (create, update, revoke, reactivate, noop)
  - PG enum ``reconciliation_delta_item_status``
      (pending, approved, rejected, applied, failed, ignored)
  - Table ``reconciliation_runs``
  - Table ``reconciliation_delta_items``

No pre-existing ``reconciliation_runs`` table is present — the Phase 0 slice kept
run counters in memory via Pydantic ``ReconciliationRunSummary`` only; no PG ORM
model or Alembic revision ever created the table.  This migration is therefore a
greenfield CREATE TABLE on both tables with zero data-migration concern.

Downgrade notes:
  downgrade only safe before any Phase 15 reconciliation runs are written.
  Dropping ``reconciliation_delta_items`` cascades via the FK constraint, but an
  explicit DROP TABLE is still issued in the correct dependency order to avoid
  ambiguity.  Dropping the enum types after the tables ensures no column still
  references them when DROP TYPE is executed.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'e3f4a5b6c7d8'
down_revision = 'd2e3f4a5b6c7'
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Enum type helpers — create_type=False so Alembic does not attempt DDL
# implicitly; we manage CREATE/DROP explicitly below.
# ---------------------------------------------------------------------------

_run_status = postgresql.ENUM(
    'running',
    'pending_apply',
    'failed',
    'applied',
    'partially_applied',
    'discarded',
    'dry_run_completed',
    name='reconciliation_run_status',
    create_type=False,
)

_delta_operation = postgresql.ENUM(
    'create',
    'update',
    'revoke',
    'reactivate',
    'noop',
    name='reconciliation_delta_operation',
    create_type=False,
)

_delta_item_status = postgresql.ENUM(
    'pending',
    'approved',
    'rejected',
    'applied',
    'failed',
    'ignored',
    name='reconciliation_delta_item_status',
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create the three enum types before any table references them.
    _run_status.create(bind, checkfirst=False)
    _delta_operation.create(bind, checkfirst=False)
    _delta_item_status.create(bind, checkfirst=False)

    # 2. reconciliation_runs (must exist before reconciliation_delta_items FK)
    op.create_table(
        'reconciliation_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('observed_batch_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('observed_snapshot_id', sa.BigInteger(), nullable=True),
        sa.Column('current_snapshot_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'status',
            postgresql.ENUM(
                'running',
                'pending_apply',
                'failed',
                'applied',
                'partially_applied',
                'discarded',
                'dry_run_completed',
                name='reconciliation_run_status',
                create_type=False,
            ),
            nullable=False,
            server_default='running',
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('revoked_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('unchanged_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_reconciliation_runs'),
        sa.ForeignKeyConstraint(
            ['application_id'],
            ['applications.id'],
            ondelete='RESTRICT',
            name='fk_reconciliation_runs_application_id',
        ),
        sa.ForeignKeyConstraint(
            ['observed_batch_id'],
            ['lake_batches.id'],
            ondelete='SET NULL',
            name='fk_reconciliation_runs_observed_batch_id',
        ),
    )
    op.create_index(
        'ix_reconciliation_runs_application_id',
        'reconciliation_runs',
        ['application_id'],
    )
    op.create_index(
        'ix_reconciliation_runs_status',
        'reconciliation_runs',
        ['status'],
    )

    # 3. reconciliation_delta_items
    op.create_table(
        'reconciliation_delta_items',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('reconciliation_run_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            'operation',
            postgresql.ENUM(
                'create',
                'update',
                'revoke',
                'reactivate',
                'noop',
                name='reconciliation_delta_operation',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('natural_key_hash', sa.CHAR(64), nullable=False),
        sa.Column('subject_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('resource_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('action_id', sa.BigInteger(), nullable=False),
        sa.Column('effect', sa.Text(), nullable=False),
        # Soft lake references — intentionally no ForeignKeyConstraint
        sa.Column('existing_fact_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('source_artifact_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('before_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('after_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            'status',
            postgresql.ENUM(
                'pending',
                'approved',
                'rejected',
                'applied',
                'failed',
                'ignored',
                name='reconciliation_delta_item_status',
                create_type=False,
            ),
            nullable=False,
            server_default='pending',
        ),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_reconciliation_delta_items'),
        sa.ForeignKeyConstraint(
            ['reconciliation_run_id'],
            ['reconciliation_runs.id'],
            ondelete='CASCADE',
            name='fk_reconciliation_delta_items_run_id',
        ),
    )
    # Composite index: supports paginated status filtering within a run (Step 9)
    # and apply-recovery scan (Step 12).  PG uses the leading column for plain
    # equality lookups, so a separate single-column index on reconciliation_run_id
    # is not needed.
    op.create_index(
        'ix_reconciliation_delta_items_run_status',
        'reconciliation_delta_items',
        ['reconciliation_run_id', 'status'],
    )


def downgrade() -> None:
    """Reverse all changes from upgrade().

    downgrade only safe before any Phase 15 reconciliation runs are written.
    """
    bind = op.get_bind()

    # 1. Drop child table first (FK dependency order)
    op.drop_index('ix_reconciliation_delta_items_run_status', table_name='reconciliation_delta_items')
    op.drop_table('reconciliation_delta_items')

    # 2. Drop parent table
    op.drop_index('ix_reconciliation_runs_status', table_name='reconciliation_runs')
    op.drop_index('ix_reconciliation_runs_application_id', table_name='reconciliation_runs')
    op.drop_table('reconciliation_runs')

    # 3. Drop the three enum types owned by this step
    _delta_item_status.drop(bind, checkfirst=False)
    _delta_operation.drop(bind, checkfirst=False)
    _run_status.drop(bind, checkfirst=False)
