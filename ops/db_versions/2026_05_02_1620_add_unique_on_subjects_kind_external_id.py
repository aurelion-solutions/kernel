# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""add_unique_on_subjects_kind_external_id

Adds UNIQUE (kind, external_id) composite constraint to the subjects table.

This is the business key for bulk-upsert: a customer with external_id="alice"
and an employee with external_id="alice" are two different rows (polymorphic
table). Phase 5 deliberately did not put a unique on external_id alone.
We add a composite unique on (kind, external_id) instead.

Pre-flight check — run before applying if this is a live database:

    SELECT kind, external_id, COUNT(*)
    FROM subjects
    GROUP BY 1, 2
    HAVING COUNT(*) > 1;

If any rows are returned, deduplicate manually before applying.

Revision ID: 15942f07987e
Revises: 9555ec2e84da
Create Date: 2026-05-02 16:20:48.938332
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '15942f07987e'
down_revision: Union[str, Sequence[str], None] = '9555ec2e84da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add UNIQUE (kind, external_id) to subjects."""
    op.create_unique_constraint('uq_subjects_kind_external_id', 'subjects', ['kind', 'external_id'])


def downgrade() -> None:
    """Drop UNIQUE (kind, external_id) from subjects."""
    op.drop_constraint('uq_subjects_kind_external_id', 'subjects', type_='unique')
