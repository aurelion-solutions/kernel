"""Phase 20 M-A — add org_units.description column.

Revision ID: d1e2f3a4b5c6
Revises: c9e4f1a20b37
Create Date: 2026-05-16 09:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c9e4f1a20b37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'org_units',
        sa.Column('description', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('org_units', 'description')
