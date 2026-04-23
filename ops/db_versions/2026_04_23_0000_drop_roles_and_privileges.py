"""drop ent_roles and ent_privileges tables

Phase 12 Step 1 hard gate: Role and Privilege cease to be first-class inventory
entities. The tables are dropped with no data migration — the system is
pre-production. The next reconciliation run repopulates AccessFact from scratch
via the artifact-first pipeline introduced in Phase 12 Step 11.

Downgrade is intentionally not implemented: recreating the tables would
contradict the Phase 12 architectural decision that roles and privileges must
exist only as AccessArtifact.artifact_type values.

Revision ID: e4b7c82d9a14
Revises: d1a4f7b9c3e5
Create Date: 2026-04-23 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = 'e4b7c82d9a14'
down_revision: str | None = 'd1a4f7b9c3e5'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table('ent_roles')
    op.drop_table('ent_privileges')


def downgrade() -> None:
    raise NotImplementedError('irreversible — Phase 12 Step 1 hard gate')
