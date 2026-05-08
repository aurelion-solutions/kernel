"""add privileged_access value to finding_kind enum

Revision ID: a7e3b9c2d4f5
Revises: 3f8a1c9d0e47
Create Date: 2026-05-06 10:00:00.000000

Adds the ``privileged_access`` value to the ``finding_kind`` Postgres enum so
ScanEngine can persist findings produced by the lens.access_risk.privileged_access
cartridge.
"""

from typing import Sequence, Union

from alembic import op

revision: str = 'a7e3b9c2d4f5'
down_revision: Union[str, None] = '3f8a1c9d0e47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE finding_kind ADD VALUE IF NOT EXISTS 'privileged_access'")


def downgrade() -> None:
    # PostgreSQL does not support removing values from an enum type. The downgrade
    # is intentionally a no-op; rolling back this migration leaves the enum value
    # in place. It is harmless because no rows reference it once findings are deleted.
    pass
