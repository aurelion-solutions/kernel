# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Add AccessArtifact permitted universal fields: raw_name, effect, valid_from, valid_until.

Phase 12 Step 9.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-04-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c4d5e6f7a8b9'
down_revision = 'b3c4d5e6f7a8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('access_artifacts', sa.Column('raw_name', sa.String(255), nullable=True))
    op.add_column('access_artifacts', sa.Column('effect', sa.Text(), nullable=True))
    op.add_column('access_artifacts', sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True))
    op.add_column('access_artifacts', sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('access_artifacts', 'valid_until')
    op.drop_column('access_artifacts', 'valid_from')
    op.drop_column('access_artifacts', 'effect')
    op.drop_column('access_artifacts', 'raw_name')
