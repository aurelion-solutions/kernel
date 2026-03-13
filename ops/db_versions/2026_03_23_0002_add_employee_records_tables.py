"""add employee_records and employee_record_attributes tables

Revision ID: g7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-03-23

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'g7b8c9d0e1f2'
down_revision: Union[str, Sequence[str], None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'employee_records',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column('external_id', sa.String(length=255), nullable=False),
        sa.Column('application_id', UUID(as_uuid=True), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ['application_id'],
            ['applications.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'employee_record_attributes',
        sa.Column('id', UUID(as_uuid=True), nullable=False),
        sa.Column('employee_record_id', UUID(as_uuid=True), nullable=False),
        sa.Column('key', sa.String(length=255), nullable=False),
        sa.Column('value', sa.String(length=1024), nullable=False),
        sa.ForeignKeyConstraint(
            ['employee_record_id'],
            ['employee_records.id'],
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'employee_record_id',
            'key',
            name='uq_employee_record_attributes_employee_record_id_key',
        ),
    )


def downgrade() -> None:
    op.drop_table('employee_record_attributes')
    op.drop_table('employee_records')
