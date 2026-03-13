"""add lake_batches table

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-22

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'lake_batches',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column('storage_provider', sa.String(length=64), nullable=False),
        sa.Column('dataset_type', sa.String(length=64), nullable=False),
        sa.Column('storage_key', sa.String(length=512), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('application_id', UUID(as_uuid=True), nullable=True),
        sa.Column('task_id', UUID(as_uuid=True), nullable=True),
        sa.Column('content_type', sa.String(length=64), nullable=True),
        sa.Column('metadata_json', JSONB, nullable=True),
        sa.ForeignKeyConstraint(['application_id'], ['applications.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'storage_provider',
            'storage_key',
            name='uq_lake_batches_storage_provider_storage_key',
        ),
    )


def downgrade() -> None:
    op.drop_table('lake_batches')
