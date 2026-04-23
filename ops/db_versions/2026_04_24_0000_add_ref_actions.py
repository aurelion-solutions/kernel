"""Phase 12 Step 2a: introduce ref_actions controlled vocabulary.

Introduces the ``ref_actions`` reference table as the canonical, controlled
vocabulary for normalized access operations. The table is seeded in-migration
with seven action slugs that form the architectural contract for
``AccessFact.action_id`` (Step 10) and handler normalization (Step 11).

The bulk insert uses a detached ``sa.table(...)`` by design — importing the
``Action`` ORM class would couple this frozen snapshot to future model drift.

Revision ID: f2a9c3b5e8d1
Revises: e4b7c82d9a14
Create Date: 2026-04-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = 'f2a9c3b5e8d1'
down_revision: str | None = 'e4b7c82d9a14'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEED_ROWS = [
    {'slug': 'read', 'description': 'Observe a resource without modifying it.'},
    {'slug': 'write', 'description': 'Modify a resource.'},
    {'slug': 'execute', 'description': 'Trigger an operation on a resource.'},
    {'slug': 'approve', 'description': 'Approve a request or transaction.'},
    {'slug': 'admin', 'description': 'Administer configuration of a resource.'},
    {'slug': 'use', 'description': 'Consume a resource as a functional user.'},
    {'slug': 'own', 'description': 'Ownership-level control of a resource.'},
]


def upgrade() -> None:
    op.create_table(
        'ref_actions',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('slug', sa.String(64), nullable=False, unique=True),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    actions_table = sa.table(
        'ref_actions',
        sa.column('slug', sa.String),
        sa.column('description', sa.String),
    )
    op.bulk_insert(actions_table, _SEED_ROWS)


def downgrade() -> None:
    op.drop_table('ref_actions')
