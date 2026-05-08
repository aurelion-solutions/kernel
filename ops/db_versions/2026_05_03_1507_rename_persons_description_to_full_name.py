"""rename persons description to full_name

Revision ID: e5be92071c84
Revises: cf1a266d2661
Create Date: 2026-05-03 15:07:45.340869

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'e5be92071c84'
down_revision: Union[str, Sequence[str], None] = 'cf1a266d2661'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('persons', 'description', new_column_name='full_name')


def downgrade() -> None:
    op.alter_column('persons', 'full_name', new_column_name='description')
