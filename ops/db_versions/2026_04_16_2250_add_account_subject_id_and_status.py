"""add account subject_id and status

Revision ID: 465335092e1e
Revises: 833a4de3198f
Create Date: 2026-04-16 22:50:59.332802

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '465335092e1e'
down_revision: str | Sequence[str] | None = '833a4de3198f'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


account_status_enum = postgresql.ENUM(
    'active',
    'suspended',
    'disabled',
    'deleted',
    'unknown',
    name='account_status',
    create_type=False,
)


def upgrade() -> None:
    # Step 1a: create PG enum type for Account.status.
    sa.Enum(
        'active',
        'suspended',
        'disabled',
        'deleted',
        'unknown',
        name='account_status',
    ).create(op.get_bind(), checkfirst=False)

    # Step 1b: add subject_id as nullable FK + add status as nullable (for backfill).
    op.add_column(
        'ent_accounts',
        sa.Column(
            'subject_id',
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        'fk_ent_accounts_subject_id_subjects',
        source_table='ent_accounts',
        referent_table='subjects',
        local_cols=['subject_id'],
        remote_cols=['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_ent_accounts_subject_id',
        'ent_accounts',
        ['subject_id'],
    )
    op.add_column(
        'ent_accounts',
        sa.Column('status', account_status_enum, nullable=True),
    )

    # Step 2: backfill status = 'unknown' for all existing rows.
    op.execute("UPDATE ent_accounts SET status = 'unknown' WHERE status IS NULL;")

    # Step 3: flip status to NOT NULL and attach server_default for future inserts.
    op.alter_column(
        'ent_accounts',
        'status',
        existing_type=account_status_enum,
        nullable=False,
        server_default=sa.text("'unknown'"),
    )


def downgrade() -> None:
    op.drop_column('ent_accounts', 'status')
    op.drop_index('ix_ent_accounts_subject_id', table_name='ent_accounts')
    op.drop_constraint(
        'fk_ent_accounts_subject_id_subjects',
        'ent_accounts',
        type_='foreignkey',
    )
    op.drop_column('ent_accounts', 'subject_id')
    sa.Enum(name='account_status').drop(op.get_bind(), checkfirst=False)
