"""add effective_grants.source_initiative_id index

Supports query use case 4 from phase_09.md §Query Use Cases:
"all grants from a specific initiative".

Revision ID: d1a4f7b9c3e5
Revises: c9d3e5f7a1b2
Create Date: 2026-04-18 01:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = 'd1a4f7b9c3e5'
down_revision: str | None = 'c9d3e5f7a1b2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No CONCURRENTLY: Alembic wraps upgrade() in a transaction; CONCURRENTLY
    # requires autocommit mode. Table is empty in all non-prod environments at
    # the time this migration lands, so the lock cost is zero. When table sizes
    # warrant concurrent reindexing, that is a separate operational task,
    # not a schema change.
    op.execute(
        'CREATE INDEX ix_effective_grants_source_initiative_id '
        'ON effective_grants (source_initiative_id)'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS ix_effective_grants_source_initiative_id')
