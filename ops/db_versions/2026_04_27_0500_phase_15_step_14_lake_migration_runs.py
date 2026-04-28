"""Phase 15 Step 14 — LakeMigrationRun table + enum types + partial unique index.

Creates:
  - PG enum ``lake_migration_dataset``  (access_artifacts, access_facts)
  - PG enum ``lake_migration_status``   (pending, running, completed, failed, cancelled)
  - Table ``lake_migration_runs``
  - Partial unique index on ``reconciliation_delta_items(reason, existing_fact_id)
    WHERE reason = 'pg_migration'``

Additionally: ALTER TABLE reconciliation_runs ALTER COLUMN application_id DROP NOT NULL
(required so the migration service can create a synthetic ReconciliationRun with
application_id=NULL for cross-app migration provenance).

Also drops the FK constraint on reconciliation_runs.application_id → applications.id
since a NULL value violates FK integrity (FK columns must point to a valid row or be NULL,
and PostgreSQL FK semantics for NOT NULL columns disallow NULL, so we must both drop NOT NULL
AND the FK to allow NULL).

Downgrade notes:
  - Restores the NOT NULL + FK constraint on reconciliation_runs.application_id.
  - Drops the partial unique index on reconciliation_delta_items.
  - Drops lake_migration_runs table.
  - Drops the two enum types.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'g1h2i3j4k5l6'
down_revision = 'f4a5b6c7d8e9'
branch_labels = None
depends_on = None

_dataset_enum = postgresql.ENUM(
    'access_artifacts',
    'access_facts',
    name='lake_migration_dataset',
    create_type=False,
)

_status_enum = postgresql.ENUM(
    'pending',
    'running',
    'completed',
    'failed',
    'cancelled',
    name='lake_migration_status',
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create enum types.
    _dataset_enum.create(bind, checkfirst=False)
    _status_enum.create(bind, checkfirst=False)

    # 2. Make reconciliation_runs.application_id nullable + drop FK.
    #    This allows synthetic migration runs with application_id=NULL.
    op.drop_constraint(
        'fk_reconciliation_runs_application_id',
        'reconciliation_runs',
        type_='foreignkey',
    )
    op.alter_column(
        'reconciliation_runs',
        'application_id',
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    # 3. lake_migration_runs table.
    op.create_table(
        'lake_migration_runs',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            'dataset',
            postgresql.ENUM(
                'access_artifacts',
                'access_facts',
                name='lake_migration_dataset',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            'status',
            postgresql.ENUM(
                'pending',
                'running',
                'completed',
                'failed',
                'cancelled',
                name='lake_migration_status',
                create_type=False,
            ),
            nullable=False,
            server_default='pending',
        ),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('last_processed_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('rows_read', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('rows_written', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('batch_size', sa.Integer(), nullable=False, server_default='5000'),
        sa.Column('error', sa.Text(), nullable=True),
        # Soft ref — no FK to reconciliation_runs (application_id may be NULL)
        sa.Column('synthetic_run_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('lake_batch_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('metadata_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_lake_migration_runs'),
        sa.ForeignKeyConstraint(
            ['lake_batch_id'],
            ['lake_batches.id'],
            ondelete='RESTRICT',
            name='fk_lake_migration_runs_lake_batch_id',
        ),
    )
    op.create_index(
        'ix_lake_migration_runs_dataset_status',
        'lake_migration_runs',
        ['dataset', 'status'],
    )
    op.create_index(
        'ix_lake_migration_runs_created_at',
        'lake_migration_runs',
        ['created_at'],
    )

    # 4. Partial unique index on reconciliation_delta_items for pg_migration idempotency.
    op.create_index(
        'uq_reconciliation_delta_items_pg_migration',
        'reconciliation_delta_items',
        ['reason', 'existing_fact_id'],
        unique=True,
        postgresql_where=sa.text("reason = 'pg_migration'"),
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse order.

    # 1. Drop partial unique index.
    op.drop_index(
        'uq_reconciliation_delta_items_pg_migration',
        table_name='reconciliation_delta_items',
    )

    # 2. Drop lake_migration_runs table.
    op.drop_index('ix_lake_migration_runs_created_at', table_name='lake_migration_runs')
    op.drop_index('ix_lake_migration_runs_dataset_status', table_name='lake_migration_runs')
    op.drop_table('lake_migration_runs')

    # 3. Restore reconciliation_runs.application_id NOT NULL + FK.
    op.alter_column(
        'reconciliation_runs',
        'application_id',
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_foreign_key(
        'fk_reconciliation_runs_application_id',
        'reconciliation_runs',
        'applications',
        ['application_id'],
        ['id'],
        ondelete='RESTRICT',
    )

    # 4. Drop enum types.
    _status_enum.drop(bind, checkfirst=False)
    _dataset_enum.drop(bind, checkfirst=False)
