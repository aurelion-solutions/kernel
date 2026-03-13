"""add application code

Revision ID: 833a4de3198f
Revises: d9ecc1641fb6
Create Date: 2026-04-15 23:28:44.490639

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '833a4de3198f'
down_revision: Union[str, Sequence[str], None] = 'd9ecc1641fb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Step 1: add nullable column.
    op.add_column(
        'applications',
        sa.Column('code', sa.String(length=64), nullable=True),
    )

    # Step 2: backfill existing rows. See TASK.md §1-Q5 for rationale.
    op.execute("""
        UPDATE applications
        SET code = substring(
            trim(both '-' from regexp_replace(lower(name), '[^a-z0-9_-]+', '-', 'g'))
            from 1 for 56
        )
        WHERE code IS NULL;
    """)
    op.execute("""
        UPDATE applications
        SET code = 'app-' || substring(id::text from 1 for 8)
        WHERE code IS NULL OR code = '';
    """)
    op.execute("""
        WITH ranked AS (
            SELECT id, code,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY created_at, id) AS rn
            FROM applications
        )
        UPDATE applications a
        SET code = substring(a.code from 1 for 55) || '-' || substring(a.id::text from 1 for 8)
        FROM ranked r
        WHERE a.id = r.id AND r.rn > 1;
    """)

    # Step 3: flip NOT NULL + UNIQUE.
    op.alter_column('applications', 'code', nullable=False)
    op.create_index('ix_applications_code', 'applications', ['code'])
    op.create_unique_constraint('uq_applications_code', 'applications', ['code'])


def downgrade() -> None:
    op.drop_constraint('uq_applications_code', 'applications', type_='unique')
    op.drop_index('ix_applications_code', table_name='applications')
    op.drop_column('applications', 'code')
