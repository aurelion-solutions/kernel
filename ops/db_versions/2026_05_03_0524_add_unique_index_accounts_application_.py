"""add unique index accounts application id username

Revision ID: cf1a266d2661
Revises: p16s15ou00001
Create Date: 2026-05-03 05:24:27.138728

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'cf1a266d2661'
down_revision: Union[str, Sequence[str], None] = 'p16s15ou00001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add unique index on (application_id, username) for bulk upsert ON CONFLICT support."""
    op.create_index(
        'ix_ent_accounts_app_username',
        'ent_accounts',
        ['application_id', 'username'],
        unique=True,
    )


def downgrade() -> None:
    """Drop unique index on (application_id, username)."""
    op.drop_index('ix_ent_accounts_app_username', table_name='ent_accounts')
