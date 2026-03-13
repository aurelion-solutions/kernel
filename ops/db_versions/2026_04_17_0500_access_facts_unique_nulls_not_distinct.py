"""access_facts unique nulls not distinct

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-04-17 05:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = 'e5f7a8b9c0d2'
down_revision: str | None = 'cc3dd4ee5ff6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace standard unique constraint with NULLS NOT DISTINCT variant."""
    op.execute('ALTER TABLE access_facts DROP CONSTRAINT uq_access_facts_natural_key')
    op.execute(
        'ALTER TABLE access_facts ADD CONSTRAINT uq_access_facts_natural_key'
        ' UNIQUE NULLS NOT DISTINCT (subject_id, account_id, resource_id, action, effect)'
    )


def downgrade() -> None:
    """Revert to standard unique constraint (NULL != NULL semantics)."""
    op.execute('ALTER TABLE access_facts DROP CONSTRAINT uq_access_facts_natural_key')
    op.execute(
        'ALTER TABLE access_facts ADD CONSTRAINT uq_access_facts_natural_key'
        ' UNIQUE (subject_id, account_id, resource_id, action, effect)'
    )
