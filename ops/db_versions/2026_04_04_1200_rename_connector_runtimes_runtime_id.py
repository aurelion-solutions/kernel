"""Rename legacy column runtime_id -> instance_id on connector_runtimes.

Runs while the table is still named ``connector_runtimes``. The table is renamed
to ``connector_instances`` in revision ``e3b9d2f1a8c4`` (following revision).

Revision ID: f2a8c1d4e9b0
Revises: 5d8c425d6a75
Create Date: 2026-04-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2a8c1d4e9b0'
down_revision: Union[str, Sequence[str], None] = 'a7f8064a31c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        'connector_runtimes',
        'runtime_id',
        new_column_name='instance_id',
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.execute(
        'ALTER INDEX ix_connector_runtimes_runtime_id RENAME TO ix_connector_runtimes_instance_id'
    )
    op.execute(
        'ALTER TABLE connector_runtimes RENAME CONSTRAINT '
        'uq_connector_runtimes_runtime_id TO uq_connector_runtimes_instance_id'
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE connector_runtimes RENAME CONSTRAINT '
        'uq_connector_runtimes_instance_id TO uq_connector_runtimes_runtime_id'
    )
    op.execute(
        'ALTER INDEX ix_connector_runtimes_instance_id RENAME TO ix_connector_runtimes_runtime_id'
    )
    op.alter_column(
        'connector_runtimes',
        'instance_id',
        new_column_name='runtime_id',
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
