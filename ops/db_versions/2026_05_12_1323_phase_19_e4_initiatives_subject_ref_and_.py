"""phase_19_e4_initiatives_subject_ref_and_replan_index

Revision ID: b1be83106f6e
Revises: 5c768d47065f
Create Date: 2026-05-12 13:23:49.855027

Changes (Phase 19 Step E4):
- Add initiatives.subject_ref (VARCHAR 256, nullable) — denormalized for
  scanner performance; populated by access_apply (F3+).
- Add initiatives.subject_type (VARCHAR 64, nullable) — 'employee' | 'nhi'.
- Add partial index idx_initiatives_replan_horizon on
  (valid_from, valid_until) WHERE valid_from > now() OR valid_until > now()
  for the scheduled replan scanner query at 10k+ initiative volumes.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b1be83106f6e'
down_revision: Union[str, Sequence[str], None] = '5c768d47065f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add subject_ref / subject_type columns and replan-horizon partial index."""
    op.add_column(
        'initiatives',
        sa.Column('subject_ref', sa.String(length=256), nullable=True),
    )
    op.add_column(
        'initiatives',
        sa.Column('subject_type', sa.String(length=64), nullable=True),
    )
    # Composite index on (valid_from, valid_until) for the E4 scanner.
    # A true partial index with WHERE valid_from > now() is not possible in
    # PostgreSQL because now() is STABLE, not IMMUTABLE.  The plain composite
    # index is sufficient: at scan time the WHERE clause filters rows; B-tree
    # on (valid_from, valid_until) covers the range scan efficiently.
    op.create_index(
        'idx_initiatives_replan_horizon',
        'initiatives',
        ['valid_from', 'valid_until'],
    )


def downgrade() -> None:
    """Remove subject_ref / subject_type columns and replan-horizon partial index."""
    op.drop_index('idx_initiatives_replan_horizon', table_name='initiatives')
    op.drop_column('initiatives', 'subject_type')
    op.drop_column('initiatives', 'subject_ref')
