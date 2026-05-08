"""add entity_type to reconciliation runs and delta items

Revision ID: 3f8a1c9d0e47
Revises: e5be92071c84
Create Date: 2026-05-03 18:22:00.000000

Phase 2 of lake-first master data architecture.

Changes:
- CREATE TYPE reconciliation_entity_type
- ADD reconciliation_runs.entity_type (default 'access_fact')
- ADD reconciliation_delta_items.entity_type (default 'access_fact')
- ADD reconciliation_delta_items.entity_id (nullable UUID — PG pk for master data rows)
- ALTER reconciliation_delta_items: make subject_id, resource_id, action_id, effect,
  natural_key_hash nullable (they are access_fact-specific; NULL for master data rows)
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = '3f8a1c9d0e47'
down_revision: Union[str, None] = 'e5be92071c84'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create the enum type
    op.execute("CREATE TYPE reconciliation_entity_type AS ENUM ('access_fact', 'person', 'org_unit', 'employee')")

    # 2. Add entity_type to reconciliation_runs
    op.add_column(
        'reconciliation_runs',
        sa.Column(
            'entity_type',
            sa.Enum('access_fact', 'person', 'org_unit', 'employee',
                    name='reconciliation_entity_type', create_type=False),
            nullable=False,
            server_default='access_fact',
        ),
    )
    op.alter_column('reconciliation_runs', 'entity_type', server_default=None)

    # 3. Add entity_type + entity_id to reconciliation_delta_items
    op.add_column(
        'reconciliation_delta_items',
        sa.Column(
            'entity_type',
            sa.Enum('access_fact', 'person', 'org_unit', 'employee',
                    name='reconciliation_entity_type', create_type=False),
            nullable=False,
            server_default='access_fact',
        ),
    )
    op.alter_column('reconciliation_delta_items', 'entity_type', server_default=None)

    op.add_column(
        'reconciliation_delta_items',
        sa.Column('entity_id', UUID(as_uuid=True), nullable=True),
    )

    # 4. Make access_fact-specific fields nullable
    op.alter_column('reconciliation_delta_items', 'natural_key_hash',
                    existing_type=sa.CHAR(64), nullable=True)
    op.alter_column('reconciliation_delta_items', 'subject_id',
                    existing_type=UUID(as_uuid=True), nullable=True)
    op.alter_column('reconciliation_delta_items', 'resource_id',
                    existing_type=UUID(as_uuid=True), nullable=True)
    op.alter_column('reconciliation_delta_items', 'action_id',
                    existing_type=sa.BigInteger(), nullable=True)
    op.alter_column('reconciliation_delta_items', 'effect',
                    existing_type=sa.Text(), nullable=True)


def downgrade() -> None:
    # Restore NOT NULL constraints (will fail if master data rows exist)
    op.alter_column('reconciliation_delta_items', 'effect',
                    existing_type=sa.Text(), nullable=False)
    op.alter_column('reconciliation_delta_items', 'action_id',
                    existing_type=sa.BigInteger(), nullable=False)
    op.alter_column('reconciliation_delta_items', 'resource_id',
                    existing_type=UUID(as_uuid=True), nullable=False)
    op.alter_column('reconciliation_delta_items', 'subject_id',
                    existing_type=UUID(as_uuid=True), nullable=False)
    op.alter_column('reconciliation_delta_items', 'natural_key_hash',
                    existing_type=sa.CHAR(64), nullable=False)

    op.drop_column('reconciliation_delta_items', 'entity_id')
    op.drop_column('reconciliation_delta_items', 'entity_type')
    op.drop_column('reconciliation_runs', 'entity_type')
    op.execute('DROP TYPE reconciliation_entity_type')
