"""Phase 19 Step A2 — Rename engine slices: reconciliation→inventory_reconcile, sync_apply→inventory_sync.

Renames:
  Tables:
    reconciliation_runs          → inventory_reconcile_runs
    reconciliation_delta_items   → inventory_reconcile_delta_items
    sync_apply_runs              → inventory_sync_runs
    sync_apply_results           → inventory_sync_results

  PG enum types:
    reconciliation_run_status         → inventory_reconcile_run_status
    reconciliation_delta_operation    → inventory_reconcile_delta_operation
    reconciliation_delta_item_status  → inventory_reconcile_delta_item_status
    reconciliation_entity_type        → inventory_reconcile_entity_type
    sync_apply_run_status             → inventory_sync_run_status
    sync_apply_run_mode               → inventory_sync_run_mode
    sync_apply_result_status          → inventory_sync_result_status

  Indexes:
    ix_reconciliation_runs_application_id    → ix_inventory_reconcile_runs_application_id
    ix_reconciliation_runs_status            → ix_inventory_reconcile_runs_status
    ix_reconciliation_delta_items_run_status → ix_inventory_reconcile_delta_items_run_status
    ix_sync_apply_runs_reconciliation_run_id → ix_inventory_sync_runs_reconciliation_run_id
    ix_sync_apply_runs_status                → ix_inventory_sync_runs_status
    ix_sync_apply_results_run_status         → ix_inventory_sync_results_run_status
    ix_sync_apply_results_delta_item_id      → ix_inventory_sync_results_delta_item_id

  FK constraints are dropped and recreated with updated names and references.

No data is moved; all renames are metadata-only in PostgreSQL.
Backward compatibility is not required (prod not running, Phase 19 internal step).

Downgrade reverses all renames in reverse order.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b3c4d5e6f789'
down_revision = 'a7e3b9d2f041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Rename PG enum types (inventory_reconcile)
    # ------------------------------------------------------------------
    bind.execute(sa.text('ALTER TYPE reconciliation_run_status RENAME TO inventory_reconcile_run_status'))
    bind.execute(sa.text('ALTER TYPE reconciliation_delta_operation RENAME TO inventory_reconcile_delta_operation'))
    bind.execute(sa.text('ALTER TYPE reconciliation_delta_item_status RENAME TO inventory_reconcile_delta_item_status'))
    bind.execute(sa.text('ALTER TYPE reconciliation_entity_type RENAME TO inventory_reconcile_entity_type'))

    # ------------------------------------------------------------------
    # 2. Rename PG enum types (inventory_sync)
    # ------------------------------------------------------------------
    bind.execute(sa.text('ALTER TYPE sync_apply_run_status RENAME TO inventory_sync_run_status'))
    bind.execute(sa.text('ALTER TYPE sync_apply_run_mode RENAME TO inventory_sync_run_mode'))
    bind.execute(sa.text('ALTER TYPE sync_apply_result_status RENAME TO inventory_sync_result_status'))

    # ------------------------------------------------------------------
    # 3. Drop FK constraints (to allow table renames)
    # ------------------------------------------------------------------
    # sync_apply_runs → reconciliation_runs FK
    op.drop_constraint(
        'fk_sync_apply_runs_reconciliation_run_id',
        'sync_apply_runs',
        type_='foreignkey',
    )
    # sync_apply_results → sync_apply_runs FK
    op.drop_constraint(
        'fk_sync_apply_results_sync_apply_run_id',
        'sync_apply_results',
        type_='foreignkey',
    )
    # sync_apply_results → reconciliation_delta_items FK
    op.drop_constraint(
        'fk_sync_apply_results_delta_item_id',
        'sync_apply_results',
        type_='foreignkey',
    )
    # reconciliation_delta_items → reconciliation_runs FK
    op.drop_constraint(
        'fk_reconciliation_delta_items_run_id',
        'reconciliation_delta_items',
        type_='foreignkey',
    )

    # ------------------------------------------------------------------
    # 4. Drop old indexes (before rename)
    # ------------------------------------------------------------------
    op.drop_index('ix_reconciliation_runs_application_id', table_name='reconciliation_runs')
    op.drop_index('ix_reconciliation_runs_status', table_name='reconciliation_runs')
    op.drop_index('ix_reconciliation_delta_items_run_status', table_name='reconciliation_delta_items')
    op.drop_index('ix_sync_apply_runs_reconciliation_run_id', table_name='sync_apply_runs')
    op.drop_index('ix_sync_apply_runs_status', table_name='sync_apply_runs')
    op.drop_index('ix_sync_apply_results_run_status', table_name='sync_apply_results')
    op.drop_index('ix_sync_apply_results_delta_item_id', table_name='sync_apply_results')

    # ------------------------------------------------------------------
    # 5. Rename tables
    # ------------------------------------------------------------------
    op.rename_table('reconciliation_runs', 'inventory_reconcile_runs')
    op.rename_table('reconciliation_delta_items', 'inventory_reconcile_delta_items')
    op.rename_table('sync_apply_runs', 'inventory_sync_runs')
    op.rename_table('sync_apply_results', 'inventory_sync_results')

    # ------------------------------------------------------------------
    # 6. Recreate indexes with new names
    # ------------------------------------------------------------------
    op.create_index('ix_inventory_reconcile_runs_application_id', 'inventory_reconcile_runs', ['application_id'])
    op.create_index('ix_inventory_reconcile_runs_status', 'inventory_reconcile_runs', ['status'])
    op.create_index(
        'ix_inventory_reconcile_delta_items_run_status',
        'inventory_reconcile_delta_items',
        ['reconciliation_run_id', 'status'],
    )
    op.create_index(
        'ix_inventory_sync_runs_reconciliation_run_id',
        'inventory_sync_runs',
        ['reconciliation_run_id'],
    )
    op.create_index('ix_inventory_sync_runs_status', 'inventory_sync_runs', ['status'])
    op.create_index(
        'ix_inventory_sync_results_run_status',
        'inventory_sync_results',
        ['sync_apply_run_id', 'status'],
    )
    op.create_index('ix_inventory_sync_results_delta_item_id', 'inventory_sync_results', ['delta_item_id'])

    # ------------------------------------------------------------------
    # 7. Recreate FK constraints with new table references
    # ------------------------------------------------------------------
    op.create_foreign_key(
        'inventory_reconcile_delta_items_reconciliation_run_id_fkey',
        'inventory_reconcile_delta_items',
        'inventory_reconcile_runs',
        ['reconciliation_run_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'inventory_sync_runs_reconciliation_run_id_fkey',
        'inventory_sync_runs',
        'inventory_reconcile_runs',
        ['reconciliation_run_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'inventory_sync_results_sync_apply_run_id_fkey',
        'inventory_sync_results',
        'inventory_sync_runs',
        ['sync_apply_run_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'inventory_sync_results_delta_item_id_fkey',
        'inventory_sync_results',
        'inventory_reconcile_delta_items',
        ['delta_item_id'],
        ['id'],
        ondelete='RESTRICT',
    )


def downgrade() -> None:
    bind = op.get_bind()

    # ------------------------------------------------------------------
    # 1. Drop FK constraints (new names)
    # ------------------------------------------------------------------
    op.drop_constraint(
        'inventory_sync_results_delta_item_id_fkey',
        'inventory_sync_results',
        type_='foreignkey',
    )
    op.drop_constraint(
        'inventory_sync_results_sync_apply_run_id_fkey',
        'inventory_sync_results',
        type_='foreignkey',
    )
    op.drop_constraint(
        'inventory_sync_runs_reconciliation_run_id_fkey',
        'inventory_sync_runs',
        type_='foreignkey',
    )
    op.drop_constraint(
        'inventory_reconcile_delta_items_reconciliation_run_id_fkey',
        'inventory_reconcile_delta_items',
        type_='foreignkey',
    )

    # ------------------------------------------------------------------
    # 2. Drop new indexes
    # ------------------------------------------------------------------
    op.drop_index('ix_inventory_sync_results_delta_item_id', table_name='inventory_sync_results')
    op.drop_index('ix_inventory_sync_results_run_status', table_name='inventory_sync_results')
    op.drop_index('ix_inventory_sync_runs_status', table_name='inventory_sync_runs')
    op.drop_index('ix_inventory_sync_runs_reconciliation_run_id', table_name='inventory_sync_runs')
    op.drop_index('ix_inventory_reconcile_delta_items_run_status', table_name='inventory_reconcile_delta_items')
    op.drop_index('ix_inventory_reconcile_runs_status', table_name='inventory_reconcile_runs')
    op.drop_index('ix_inventory_reconcile_runs_application_id', table_name='inventory_reconcile_runs')

    # ------------------------------------------------------------------
    # 3. Rename tables back
    # ------------------------------------------------------------------
    op.rename_table('inventory_sync_results', 'sync_apply_results')
    op.rename_table('inventory_sync_runs', 'sync_apply_runs')
    op.rename_table('inventory_reconcile_delta_items', 'reconciliation_delta_items')
    op.rename_table('inventory_reconcile_runs', 'reconciliation_runs')

    # ------------------------------------------------------------------
    # 4. Recreate old indexes
    # ------------------------------------------------------------------
    op.create_index('ix_reconciliation_runs_application_id', 'reconciliation_runs', ['application_id'])
    op.create_index('ix_reconciliation_runs_status', 'reconciliation_runs', ['status'])
    op.create_index(
        'ix_reconciliation_delta_items_run_status',
        'reconciliation_delta_items',
        ['reconciliation_run_id', 'status'],
    )
    op.create_index('ix_sync_apply_runs_reconciliation_run_id', 'sync_apply_runs', ['reconciliation_run_id'])
    op.create_index('ix_sync_apply_runs_status', 'sync_apply_runs', ['status'])
    op.create_index(
        'ix_sync_apply_results_run_status',
        'sync_apply_results',
        ['sync_apply_run_id', 'status'],
    )
    op.create_index('ix_sync_apply_results_delta_item_id', 'sync_apply_results', ['delta_item_id'])

    # ------------------------------------------------------------------
    # 5. Recreate old FK constraints
    # ------------------------------------------------------------------
    op.create_foreign_key(
        'fk_reconciliation_delta_items_run_id',
        'reconciliation_delta_items',
        'reconciliation_runs',
        ['reconciliation_run_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_sync_apply_runs_reconciliation_run_id',
        'sync_apply_runs',
        'reconciliation_runs',
        ['reconciliation_run_id'],
        ['id'],
        ondelete='RESTRICT',
    )
    op.create_foreign_key(
        'fk_sync_apply_results_sync_apply_run_id',
        'sync_apply_results',
        'sync_apply_runs',
        ['sync_apply_run_id'],
        ['id'],
        ondelete='CASCADE',
    )
    op.create_foreign_key(
        'fk_sync_apply_results_delta_item_id',
        'sync_apply_results',
        'reconciliation_delta_items',
        ['delta_item_id'],
        ['id'],
        ondelete='RESTRICT',
    )

    # ------------------------------------------------------------------
    # 6. Rename PG enum types back (inventory_sync)
    # ------------------------------------------------------------------
    bind.execute(sa.text('ALTER TYPE inventory_sync_result_status RENAME TO sync_apply_result_status'))
    bind.execute(sa.text('ALTER TYPE inventory_sync_run_mode RENAME TO sync_apply_run_mode'))
    bind.execute(sa.text('ALTER TYPE inventory_sync_run_status RENAME TO sync_apply_run_status'))

    # ------------------------------------------------------------------
    # 7. Rename PG enum types back (inventory_reconcile)
    # ------------------------------------------------------------------
    bind.execute(sa.text('ALTER TYPE inventory_reconcile_entity_type RENAME TO reconciliation_entity_type'))
    bind.execute(sa.text('ALTER TYPE inventory_reconcile_delta_item_status RENAME TO reconciliation_delta_item_status'))
    bind.execute(sa.text('ALTER TYPE inventory_reconcile_delta_operation RENAME TO reconciliation_delta_operation'))
    bind.execute(sa.text('ALTER TYPE inventory_reconcile_run_status RENAME TO reconciliation_run_status'))
