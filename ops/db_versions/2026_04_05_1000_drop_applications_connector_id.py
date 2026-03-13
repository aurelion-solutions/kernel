"""drop applications.connector_id (unused; routing uses required_connector_tags + connector_instances)

Revision ID: c1d2e3f4a5b6
Revises: e3b9d2f1a8c4
Create Date: 2026-04-05 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'e3b9d2f1a8c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(op.f('ix_applications_connector_id'), table_name='applications')
    op.drop_column('applications', 'connector_id')


def downgrade() -> None:
    op.add_column(
        'applications',
        sa.Column(
            'connector_id',
            sa.String(length=255),
            server_default=sa.text('gen_random_uuid()::text'),
            nullable=False,
        ),
    )
    op.create_index(
        op.f('ix_applications_connector_id'),
        'applications',
        ['connector_id'],
        unique=True,
    )
