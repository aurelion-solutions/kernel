# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""phase_16_step_15_org_units

Adds the org_units table and the employees.org_unit_id nullable FK column.

Revision ID: a1b2c3d4e5f6
Revises: 15942f07987e
Create Date: 2026-05-02 20:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'p16s15ou00001'
down_revision: Union[str, Sequence[str], None] = '15942f07987e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create org_units table and add org_unit_id FK column to employees."""
    # --- org_units table ---------------------------------------------------
    op.create_table(
        'org_units',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column('external_id', sa.String(255), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column(
            'parent_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('org_units.id', ondelete='SET NULL'),
            nullable=True,
        ),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_unique_constraint('uq_org_units_external_id', 'org_units', ['external_id'])

    # --- employees.org_unit_id nullable FK column --------------------------
    op.add_column(
        'employees',
        sa.Column(
            'org_unit_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('org_units.id', ondelete='SET NULL'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop org_unit_id from employees and drop org_units table."""
    op.drop_column('employees', 'org_unit_id')
    op.drop_constraint('uq_org_units_external_id', 'org_units', type_='unique')
    op.drop_table('org_units')
