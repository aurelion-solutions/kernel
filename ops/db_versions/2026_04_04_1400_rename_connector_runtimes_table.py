"""Rename legacy table connector_runtimes -> connector_instances.

Aligns the physical table with the ``ConnectorInstance`` domain model and API
path ``/connector-instances``. Downgrade restores legacy names for rollback only.

Revision ID: e3b9d2f1a8c4
Revises: f2a8c1d4e9b0
Create Date: 2026-04-04 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


revision: str = 'e3b9d2f1a8c4'
down_revision: Union[str, Sequence[str], None] = 'f2a8c1d4e9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table('connector_runtimes', 'connector_instances')
    op.execute(
        'ALTER INDEX ix_connector_runtimes_instance_id RENAME TO ix_connector_instances_instance_id'
    )
    op.execute(
        'ALTER TABLE connector_instances RENAME CONSTRAINT '
        'uq_connector_runtimes_instance_id TO uq_connector_instances_instance_id'
    )


def downgrade() -> None:
    op.execute(
        'ALTER TABLE connector_instances RENAME CONSTRAINT '
        'uq_connector_instances_instance_id TO uq_connector_runtimes_instance_id'
    )
    op.execute(
        'ALTER INDEX ix_connector_instances_instance_id RENAME TO ix_connector_runtimes_instance_id'
    )
    op.rename_table('connector_instances', 'connector_runtimes')
