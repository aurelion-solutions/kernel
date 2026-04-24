"""Phase 12 Step 6: add resource_type / resource_key identity columns + UNIQUE to resources.

Adds the normalized identity triple (application_id, resource_type, resource_key) to the
resources table. Migration is safe for empty and non-empty databases:

1. Add both columns as nullable.
2. Backfill: resource_type = kind, resource_key = external_id for any existing row.
3. ALTER to NOT NULL.
4. Add UNIQUE constraint.

Revision ID: a1b2c3d4e5f6
Revises: f2a9c3b5e8d1
Create Date: 2026-04-24 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'a1b2c4d5e6f7'
down_revision: str | None = 'f2a9c3b5e8d1'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('resources', sa.Column('resource_type', sa.String(255), nullable=True))
    op.add_column('resources', sa.Column('resource_key', sa.String(1024), nullable=True))

    op.execute(
        'UPDATE resources SET resource_type = kind, resource_key = external_id '
        'WHERE resource_type IS NULL OR resource_key IS NULL'
    )

    op.alter_column(
        'resources',
        'resource_type',
        existing_type=sa.String(255),
        existing_nullable=True,
        nullable=False,
    )
    op.alter_column(
        'resources',
        'resource_key',
        existing_type=sa.String(1024),
        existing_nullable=True,
        nullable=False,
    )

    op.create_unique_constraint(
        'uq_resources_application_id_resource_type_resource_key',
        'resources',
        ['application_id', 'resource_type', 'resource_key'],
    )


def downgrade() -> None:
    op.drop_constraint(
        'uq_resources_application_id_resource_type_resource_key',
        'resources',
        type_='unique',
    )
    op.drop_column('resources', 'resource_key')
    op.drop_column('resources', 'resource_type')
