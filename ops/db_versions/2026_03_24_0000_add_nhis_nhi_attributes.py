"""add nhis and nhi_attributes tables

Revision ID: h8c9d0e1f2a3
Revises: g7b8c9d0e1f2
Create Date: 2026-03-24

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'h8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'g7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'nhis',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column('external_id', sa.String(length=255), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('kind', sa.String(length=64), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('is_locked', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('owner_employee_id', UUID(as_uuid=True), nullable=True),
        sa.Column('application_id', UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(
            ['owner_employee_id'],
            ['employees.id'],
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['application_id'],
            ['applications.id'],
            ondelete='SET NULL',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'nhi_attributes',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column('nhi_id', UUID(as_uuid=True), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=False),
        sa.Column('value', sa.String(length=1024), nullable=False),
        sa.ForeignKeyConstraint(
            ['nhi_id'],
            ['nhis.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'nhi_id',
            'key',
            name='uq_nhi_attributes_nhi_id_key',
        ),
    )


def downgrade() -> None:
    op.drop_table('nhi_attributes')
    op.drop_table('nhis')
