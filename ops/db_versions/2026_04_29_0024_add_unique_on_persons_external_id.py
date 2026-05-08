"""add unique on persons external id

Revision ID: 9372cefb0a63
Revises: e6caebd56143
Create Date: 2026-04-29 00:24:08.456630

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '9372cefb0a63'
down_revision: str | Sequence[str] | None = 'e6caebd56143'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add UNIQUE constraint on persons.external_id."""
    op.create_unique_constraint('uq_persons_external_id', 'persons', ['external_id'])


def downgrade() -> None:
    """Drop UNIQUE constraint on persons.external_id."""
    op.drop_constraint('uq_persons_external_id', 'persons', type_='unique')
