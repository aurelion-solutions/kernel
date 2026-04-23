# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""artifact_binding_polymorphic_target

Collapse three nullable FK columns (access_fact_id, resource_id, account_id) into a
polymorphic pair (target_type String(64), target_id UUID).

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-04-24 04:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: str = 'c4d5e6f7a8b9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop all existing rows — pre-production, no data migration needed.
    # Rationale: the old three-FK rows have no clean projection onto the new shape
    # without inferring target_type; reconciliation will repopulate on the next run.
    op.execute('DELETE FROM artifact_bindings')

    # Drop the CHECK constraint that enforced at-least-one-FK-non-null.
    op.drop_constraint('chk_artifact_binding_has_target', 'artifact_bindings', type_='check')

    # Drop the three per-target FK indexes.
    op.drop_index('ix_artifact_bindings_access_fact_id', table_name='artifact_bindings')
    op.drop_index('ix_artifact_bindings_resource_id', table_name='artifact_bindings')
    op.drop_index('ix_artifact_bindings_account_id', table_name='artifact_bindings')

    # Drop the three nullable FK columns (FK constraints are dropped with the columns).
    op.drop_column('artifact_bindings', 'access_fact_id')
    op.drop_column('artifact_bindings', 'resource_id')
    op.drop_column('artifact_bindings', 'account_id')

    # Add the polymorphic pair — safe to be NOT NULL because the table is now empty.
    op.add_column(
        'artifact_bindings',
        sa.Column('target_type', sa.String(64), nullable=False),
    )
    op.add_column(
        'artifact_bindings',
        sa.Column('target_id', postgresql.UUID(as_uuid=True), nullable=False),
    )

    # UNIQUE (artifact_id, target_type, target_id) — dedup / idempotency lever for
    # Step 14 reconciliation (ON CONFLICT DO NOTHING semantics).
    op.create_unique_constraint(
        'uq_artifact_bindings_artifact_id_target_type_target_id',
        'artifact_bindings',
        ['artifact_id', 'target_type', 'target_id'],
    )

    # Composite index supporting the ?target_type=&target_id= query pattern.
    op.create_index(
        'ix_artifact_bindings_target',
        'artifact_bindings',
        ['target_type', 'target_id'],
    )


def downgrade() -> None:
    # Drop new index and UNIQUE constraint.
    op.drop_index('ix_artifact_bindings_target', table_name='artifact_bindings')
    op.drop_constraint(
        'uq_artifact_bindings_artifact_id_target_type_target_id',
        'artifact_bindings',
        type_='unique',
    )

    # Drop the two new columns.
    op.drop_column('artifact_bindings', 'target_id')
    op.drop_column('artifact_bindings', 'target_type')

    # Restore the three nullable FK columns.
    op.add_column(
        'artifact_bindings',
        sa.Column(
            'access_fact_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('access_facts.id', ondelete='CASCADE'),
            nullable=True,
        ),
    )
    op.add_column(
        'artifact_bindings',
        sa.Column(
            'resource_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('resources.id', ondelete='CASCADE'),
            nullable=True,
        ),
    )
    op.add_column(
        'artifact_bindings',
        sa.Column(
            'account_id',
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey('ent_accounts.id', ondelete='CASCADE'),
            nullable=True,
        ),
    )

    # Restore the CHECK constraint.
    op.create_check_constraint(
        'chk_artifact_binding_has_target',
        'artifact_bindings',
        'COALESCE(access_fact_id::text, resource_id::text, account_id::text) IS NOT NULL',
    )

    # Restore the three per-target FK indexes.
    op.create_index(
        'ix_artifact_bindings_access_fact_id',
        'artifact_bindings',
        ['access_fact_id'],
    )
    op.create_index(
        'ix_artifact_bindings_resource_id',
        'artifact_bindings',
        ['resource_id'],
    )
    op.create_index(
        'ix_artifact_bindings_account_id',
        'artifact_bindings',
        ['account_id'],
    )
