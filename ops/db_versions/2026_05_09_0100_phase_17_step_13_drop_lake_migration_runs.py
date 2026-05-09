"""Phase 17 Step 13 — Drop lake_migration_runs table + enum types + partial unique index.

This is the retirement migration for the engines/lake_migration slice.

Drops:
  - Partial unique index ``uq_reconciliation_delta_items_pg_migration`` on
    ``reconciliation_delta_items(reason, existing_fact_id) WHERE reason = 'pg_migration'``
  - Indexes ``ix_lake_migration_runs_created_at``,
    ``ix_lake_migration_runs_dataset_status`` on ``lake_migration_runs``
  - Table ``lake_migration_runs``
  - PG enum ``lake_migration_status``
  - PG enum ``lake_migration_dataset``

Does NOT restore ``reconciliation_runs.application_id NOT NULL`` or its FK —
those were made nullable deliberately in Phase 15 Step 14 for cross-app
provenance and remain nullable.

Downgrade recreates the table, enums, and indexes (schema-level reversal only;
historical row data is intentionally not recoverable).
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'c2f5a8d91b04'
down_revision = 'b1e4f7c20d83'
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

    # 1. Drop partial unique index on reconciliation_delta_items.
    op.drop_index(
        'uq_reconciliation_delta_items_pg_migration',
        table_name='reconciliation_delta_items',
    )

    # 2. Drop indexes on lake_migration_runs.
    op.drop_index('ix_lake_migration_runs_created_at', table_name='lake_migration_runs')
    op.drop_index('ix_lake_migration_runs_dataset_status', table_name='lake_migration_runs')

    # 3. Drop the lake_migration_runs table.
    op.drop_table('lake_migration_runs')

    # 4. Drop enum types (status first, dataset second).
    _status_enum.drop(bind, checkfirst=True)
    _dataset_enum.drop(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse: recreate enums, table, indexes.

    # 1. Create enum types.
    _dataset_enum.create(bind, checkfirst=False)
    _status_enum.create(bind, checkfirst=False)

    # 2. Recreate lake_migration_runs table.
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

    # 3. Recreate partial unique index on reconciliation_delta_items.
    op.create_index(
        'uq_reconciliation_delta_items_pg_migration',
        'reconciliation_delta_items',
        ['reason', 'existing_fact_id'],
        unique=True,
        postgresql_where=sa.text("reason = 'pg_migration'"),
    )
