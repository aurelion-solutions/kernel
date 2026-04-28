"""Phase 15 Step 4 — extend lake_batches for Iceberg-origin rows.

Changes applied:
  1. Add nullable columns ``iceberg_namespace``, ``iceberg_table``, ``snapshot_id``.
  2. Relax ``storage_provider`` and ``storage_key`` to nullable — Iceberg writes do
     not produce file-storage coordinates.
  3. Replace the legacy UNIQUE constraint on ``(storage_provider, storage_key)`` with
     a *partial* unique index restricted to rows where both columns are NOT NULL.
     The partial index name is ``uq_lake_batches_storage_provider_storage_key_active``
     so that both the Alembic downgrade and ``Base.metadata.create_all`` (used in tests)
     produce the same identifier.

Downgrade notes:
  Downgrade is only safe **before any Iceberg-origin batches are written**.
  If rows exist where ``storage_provider IS NULL`` or ``storage_key IS NULL``,
  the ``ALTER COLUMN … NOT NULL`` steps in ``downgrade()`` will raise a
  ``ProgrammingError`` from PostgreSQL.  This is intentional: downgrade in
  production is only performed before Step 5 ships any Iceberg writes, and the
  failure is the correct guard against silent data loss.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa

revision = 'd2e3f4a5b6c7'
down_revision = '1ce9b6a5d2c1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('lake_batches', sa.Column('iceberg_namespace', sa.String(64), nullable=True))
    op.add_column('lake_batches', sa.Column('iceberg_table', sa.String(128), nullable=True))
    op.add_column('lake_batches', sa.Column('snapshot_id', sa.BigInteger(), nullable=True))
    op.alter_column('lake_batches', 'storage_provider', existing_type=sa.String(64), nullable=True)
    op.alter_column('lake_batches', 'storage_key', existing_type=sa.String(512), nullable=True)
    op.drop_constraint(
        'uq_lake_batches_storage_provider_storage_key',
        'lake_batches',
        type_='unique',
    )
    op.create_index(
        'uq_lake_batches_storage_provider_storage_key_active',
        'lake_batches',
        ['storage_provider', 'storage_key'],
        unique=True,
        postgresql_where=sa.text('storage_provider IS NOT NULL AND storage_key IS NOT NULL'),
    )


def downgrade() -> None:
    """Reverse all changes from upgrade().

    WARNING: downgrade only safe before any Iceberg-origin batches are written.
    Rows with NULL storage_provider or storage_key will cause the ALTER COLUMN
    NOT NULL steps to fail with a PostgreSQL error.
    """
    op.drop_index(
        'uq_lake_batches_storage_provider_storage_key_active',
        table_name='lake_batches',
    )
    op.create_unique_constraint(
        'uq_lake_batches_storage_provider_storage_key',
        'lake_batches',
        ['storage_provider', 'storage_key'],
    )
    op.alter_column('lake_batches', 'storage_key', existing_type=sa.String(512), nullable=False)
    op.alter_column('lake_batches', 'storage_provider', existing_type=sa.String(64), nullable=False)
    op.drop_column('lake_batches', 'snapshot_id')
    op.drop_column('lake_batches', 'iceberg_table')
    op.drop_column('lake_batches', 'iceberg_namespace')
