# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""access_fact_current_state_store

Reshape access_facts into a current-state store:
- DELETE all existing rows (pre-production drop; reconciliation rebuilds from artifacts in Step 14)
- Drop action enum column (keep PG enum type — still owned by effective_grants.action)
- Add action_id BIGINT FK → ref_actions(id) ON DELETE RESTRICT
- Add is_active BOOLEAN NOT NULL DEFAULT TRUE
- Add revoked_at TIMESTAMPTZ NULL
- Add observed_at TIMESTAMPTZ NOT NULL
- Replace uq_access_facts_natural_key with two partial unique indexes on active rows only

PG enum type `action` is NOT dropped — it is shared with effective_grants.action
(Enum(Action, name='action', create_type=False) in capabilities/effective_access/models.py).
ENUM_AUDIT: grep -RIn "Enum(Action," src/ confirms EffectiveGrant is the only remaining consumer.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-04-24 05:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e6f7a8b9c0d1'
down_revision = 'd5e6f7a8b9c0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Discard pre-production rows — reconciliation rebuilds via Step 14 handler pipeline
    op.execute('DELETE FROM access_facts')

    # 2. Drop old index and constraint on the action enum column
    op.drop_index('ix_access_facts_action', table_name='access_facts')
    op.drop_constraint('uq_access_facts_natural_key', 'access_facts', type_='unique')

    # 3. Drop the action enum column (PG enum type 'action' stays — shared with effective_grants)
    op.drop_column('access_facts', 'action')

    # 4. Add action_id BIGINT FK → ref_actions(id) ON DELETE RESTRICT
    # Safe NOT NULL because table is empty after DELETE above.
    op.add_column('access_facts', sa.Column('action_id', sa.BigInteger(), nullable=False))
    op.create_foreign_key(
        'fk_access_facts_action_id_ref_actions',
        'access_facts',
        'ref_actions',
        ['action_id'],
        ['id'],
        ondelete='RESTRICT',
    )

    # 5. Add lifecycle columns
    op.add_column(
        'access_facts',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        'access_facts',
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    )
    # observed_at is NOT NULL and caller-supplied — safe because table is empty
    op.add_column(
        'access_facts',
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
    )

    # 6. Partial unique indexes on active rows only (replaces the non-partial uq_access_facts_natural_key)
    op.create_index(
        'uq_access_facts_active_account_key',
        'access_facts',
        ['account_id', 'resource_id', 'action_id'],
        unique=True,
        postgresql_where=sa.text('account_id IS NOT NULL AND is_active = true'),
    )
    op.create_index(
        'uq_access_facts_active_subject_key',
        'access_facts',
        ['subject_id', 'resource_id', 'action_id'],
        unique=True,
        postgresql_where=sa.text('account_id IS NULL AND is_active = true'),
    )

    # 7. Supporting indexes
    op.create_index('ix_access_facts_action_id', 'access_facts', ['action_id'])
    op.create_index(
        'ix_access_facts_is_active',
        'access_facts',
        ['is_active'],
        postgresql_where=sa.text('is_active = false'),
    )


def downgrade() -> None:
    # Drop new indexes
    op.drop_index('ix_access_facts_is_active', table_name='access_facts')
    op.drop_index('ix_access_facts_action_id', table_name='access_facts')
    op.drop_index('uq_access_facts_active_subject_key', table_name='access_facts')
    op.drop_index('uq_access_facts_active_account_key', table_name='access_facts')

    # Drop new columns and FK
    op.drop_column('access_facts', 'observed_at')
    op.drop_column('access_facts', 'revoked_at')
    op.drop_column('access_facts', 'is_active')
    op.drop_constraint('fk_access_facts_action_id_ref_actions', 'access_facts', type_='foreignkey')
    op.drop_column('access_facts', 'action_id')

    # Restore action enum column (create_type=False — PG type 'action' was never dropped)
    op.add_column(
        'access_facts',
        sa.Column(
            'action',
            sa.Enum(name='action', create_type=False),
            nullable=False,
            server_default='read',
        ),
    )
    # Remove server_default now that the column exists
    op.alter_column('access_facts', 'action', server_default=None)

    # Restore old unique constraint and index
    op.create_unique_constraint(
        'uq_access_facts_natural_key',
        'access_facts',
        ['subject_id', 'account_id', 'resource_id', 'action', 'effect'],
        postgresql_nulls_not_distinct=True,
    )
    op.create_index('ix_access_facts_action', 'access_facts', ['action'])
