"""add unique on employees person_id

Revision ID: 9555ec2e84da
Revises: 9372cefb0a63
Create Date: 2026-04-29 00:54:04.798765

"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9555ec2e84da'
down_revision: Union[str, Sequence[str], None] = '9372cefb0a63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add UNIQUE constraint on employees.person_id.

    Pre-flight check for production:
        SELECT person_id, COUNT(*) FROM employees GROUP BY 1 HAVING COUNT(*) > 1;
    If any rows returned — deduplicate manually before applying this migration.
    """
    op.create_unique_constraint('uq_employees_person_id', 'employees', ['person_id'])


def downgrade() -> None:
    """Drop UNIQUE constraint on employees.person_id."""
    op.drop_constraint('uq_employees_person_id', 'employees', type_='unique')
