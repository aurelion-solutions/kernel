# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase 19 Step B1: add descriptor column to connector_instances.

Revision ID: 4a20905133bc
Revises: c4d5e6f78901
Create Date: 2026-05-12 11:08:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '4a20905133bc'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f78901'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable JSONB descriptor column to connector_instances."""
    op.add_column(
        'connector_instances',
        sa.Column('descriptor', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Remove descriptor column from connector_instances."""
    op.drop_column('connector_instances', 'descriptor')
