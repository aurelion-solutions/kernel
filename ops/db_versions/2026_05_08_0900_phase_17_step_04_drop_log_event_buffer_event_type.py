# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase 17 Step 4 — drop log_event_buffer.event_type column.

The ``event_type`` field on ``LogEvent`` was deprecated since Phase 10 Step 23.
No live producer populates it. The column is dropped here; historical values are
NOT recovered by downgrade (acceptable given the deprecation precedent).

Downgrade adds the column back with ``server_default=''`` to satisfy NOT NULL on
round-trip against pre-existing rows.

Revision ID: b1e4f7c20d83
Revises: a7e3b9c2d4f5
Create Date: 2026-05-08 09:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = 'b1e4f7c20d83'
down_revision: str = 'a7e3b9c2d4f5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('log_event_buffer', 'event_type')


def downgrade() -> None:
    op.add_column(
        'log_event_buffer',
        sa.Column(
            'event_type',
            sa.String(length=255),
            nullable=False,
            server_default='',
        ),
    )
