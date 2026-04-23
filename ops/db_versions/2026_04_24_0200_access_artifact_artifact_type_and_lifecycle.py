"""access_artifact: rename source_kind -> artifact_type + add lifecycle columns + UNIQUE.

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-04-24 02:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b3c4d5e6f7a8'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Rename column source_kind -> artifact_type
    op.alter_column(
        'access_artifacts',
        'source_kind',
        new_column_name='artifact_type',
        existing_type=sa.String(255),
        existing_nullable=False,
    )

    # 2. Rename index
    op.execute('ALTER INDEX ix_access_artifacts_source_kind RENAME TO ix_access_artifacts_artifact_type')

    # 3. Add observed_at as nullable first (backfill from ingested_at)
    op.add_column(
        'access_artifacts',
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=True),
    )

    # 4. Backfill
    op.execute('UPDATE access_artifacts SET observed_at = ingested_at WHERE observed_at IS NULL')

    # 5. Make NOT NULL
    op.alter_column(
        'access_artifacts',
        'observed_at',
        nullable=False,
        existing_type=sa.DateTime(timezone=True),
        existing_nullable=True,
    )

    # 6. Add is_active
    op.add_column(
        'access_artifacts',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    # 7. Add tombstoned_at
    op.add_column(
        'access_artifacts',
        sa.Column('tombstoned_at', sa.DateTime(timezone=True), nullable=True),
    )

    # 8. Add UNIQUE constraint
    op.create_unique_constraint(
        'uq_access_artifacts_application_id_artifact_type_external_id',
        'access_artifacts',
        ['application_id', 'artifact_type', 'external_id'],
    )


def downgrade() -> None:
    # 1. Drop UNIQUE constraint
    op.drop_constraint(
        'uq_access_artifacts_application_id_artifact_type_external_id',
        'access_artifacts',
        type_='unique',
    )

    # 2. Drop tombstoned_at
    op.drop_column('access_artifacts', 'tombstoned_at')

    # 3. Drop is_active
    op.drop_column('access_artifacts', 'is_active')

    # 4. Drop observed_at
    op.drop_column('access_artifacts', 'observed_at')

    # 5. Rename index back
    op.execute('ALTER INDEX ix_access_artifacts_artifact_type RENAME TO ix_access_artifacts_source_kind')

    # 6. Rename column back
    op.alter_column(
        'access_artifacts',
        'artifact_type',
        new_column_name='source_kind',
        existing_type=sa.String(255),
        existing_nullable=False,
    )
