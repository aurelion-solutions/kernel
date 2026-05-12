"""add access_plan models and invited account status

Revision ID: 5c768d47065f
Revises: 4a20905133bc
Create Date: 2026-05-12 11:22:14.197843

Changes:
- Add account_status enum value 'invited'  (Phase 19 D1)
- Create access_plans table with partial indexes
- Create access_plan_items table
- Create access_plan_deps table
- Create plan_item_executions table
- Create access_apply_active table (subject-level apply lease)
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5c768d47065f'
down_revision: Union[str, Sequence[str], None] = '4a20905133bc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ------------------------------------------------------------------
    # Extend account_status enum with 'invited'
    # Uses the Postgres ALTER TYPE ... ADD VALUE approach (non-transactional).
    # ------------------------------------------------------------------
    op.execute("ALTER TYPE account_status ADD VALUE IF NOT EXISTS 'invited'")

    # ------------------------------------------------------------------
    # New PG enum types for access_plan engine
    # ------------------------------------------------------------------
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE access_plan_status AS ENUM ('active', 'superseded', 'cancelled', 'invalid');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE plan_invalidation_reason AS ENUM ('structural', 'stale_after_apply');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE plan_item_kind AS ENUM (
                'account_create', 'account_invite', 'account_activate',
                'account_suspend', 'account_disable',
                'grant_role', 'revoke_role',
                'group_add', 'group_remove',
                'entitlement_attach', 'entitlement_detach'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE plan_item_execution_status AS ENUM ('proposed', 'executing', 'done', 'failed');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )
    op.execute(
        """
        DO $$ BEGIN
            CREATE TYPE plan_item_failure_reason AS ENUM (
                'precondition', 'apply_error', 'verify_mismatch', 'verify_timeout'
            );
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    # ------------------------------------------------------------------
    # access_plans — immutable plan header
    # ------------------------------------------------------------------
    op.create_table(
        'access_plans',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column(
            'subject_ref',
            sa.String(length=512),
            nullable=False,
            comment='Opaque subject identifier (employee_id or nhi_id as string)',
        ),
        sa.Column(
            'subject_type',
            sa.String(length=64),
            nullable=False,
            comment='employee | nhi',
        ),
        sa.Column('idempotency_key', sa.String(length=512), nullable=True),
        sa.Column(
            'content_hash',
            sa.String(length=64),
            nullable=False,
            comment='SHA-256 of the planning input snapshot for hash-based dedup',
        ),
        sa.Column(
            'status',
            sa.Enum(
                'active', 'superseded', 'cancelled', 'invalid',
                name='access_plan_status',
                create_type=False,
            ),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            'invalidation_reason',
            sa.Enum(
                'structural', 'stale_after_apply',
                name='plan_invalidation_reason',
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column('invalidated_by_plan_id', sa.UUID(), nullable=True),
        sa.Column(
            'requires_confirmation',
            sa.Boolean(),
            server_default=sa.text('false'),
            nullable=False,
            comment='True when destructive threshold exceeded; caller must pass ?confirm_destructive=true',
        ),
        sa.Column('supersedes_plan_id', sa.UUID(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ['invalidated_by_plan_id'], ['access_plans.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['supersedes_plan_id'], ['access_plans.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    # Partial index for auto-invalidation: WHERE status = 'active'
    op.create_index(
        'ix_access_plans_subject_ref_active',
        'access_plans',
        ['subject_ref', 'status'],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    # Traversal of supersedes chain
    op.create_index(
        'ix_access_plans_supersedes_plan_id',
        'access_plans',
        ['supersedes_plan_id'],
        unique=False,
    )
    # Unique idempotency_key (only where not null)
    op.create_index(
        'uq_access_plans_idempotency_key',
        'access_plans',
        ['idempotency_key'],
        unique=True,
        postgresql_where=sa.text('idempotency_key IS NOT NULL'),
    )

    # ------------------------------------------------------------------
    # access_plan_items — immutable per-plan operations
    # ------------------------------------------------------------------
    op.create_table(
        'access_plan_items',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column(
            'kind',
            sa.Enum(
                'account_create', 'account_invite', 'account_activate',
                'account_suspend', 'account_disable',
                'grant_role', 'revoke_role',
                'group_add', 'group_remove',
                'entitlement_attach', 'entitlement_detach',
                name='plan_item_kind',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            'application',
            sa.String(length=256),
            nullable=False,
            comment='Application code / connector identifier',
        ),
        sa.Column(
            'account_ref',
            sa.String(length=512),
            nullable=True,
            comment='Opaque account identifier in the target system, if known',
        ),
        sa.Column(
            'target_descriptor',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment='Role / group / entitlement descriptor for the target system',
        ),
        sa.Column(
            'initiatives',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment='Initiative objects from PDP decision (for grant path)',
        ),
        sa.Column(
            'initiative_refs',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment='UUIDs of existing Initiative rows to close (for revoke path)',
        ),
        sa.Column(
            'policy_rule_refs',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment='rule_id strings from PDP reasons',
        ),
        sa.Column(
            'decision_snapshot',
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
            comment='Immutable copy of PDP Decision at planning time (for audit + Phase 20 attestation UI)',
        ),
        sa.ForeignKeyConstraint(['plan_id'], ['access_plans.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_access_plan_items_plan_id', 'access_plan_items', ['plan_id'], unique=False)

    # ------------------------------------------------------------------
    # access_plan_deps — immutable DAG edges
    # ------------------------------------------------------------------
    op.create_table(
        'access_plan_deps',
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('item_id', sa.UUID(), nullable=False),
        sa.Column('requires_item_id', sa.UUID(), nullable=False),
        sa.ForeignKeyConstraint(
            ['item_id'], ['access_plan_items.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['plan_id'], ['access_plans.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['requires_item_id'], ['access_plan_items.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('plan_id', 'item_id', 'requires_item_id'),
    )
    op.create_index(
        'ix_access_plan_deps_item_id', 'access_plan_deps', ['plan_id', 'item_id'], unique=False
    )

    # ------------------------------------------------------------------
    # plan_item_executions — mutable execution state per item
    # ------------------------------------------------------------------
    op.create_table(
        'plan_item_executions',
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column('item_id', sa.UUID(), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'proposed', 'executing', 'done', 'failed',
                name='plan_item_execution_status',
                create_type=False,
            ),
            server_default=sa.text("'proposed'"),
            nullable=False,
        ),
        sa.Column(
            'failure_reason',
            sa.Enum(
                'precondition', 'apply_error', 'verify_mismatch', 'verify_timeout',
                name='plan_item_failure_reason',
                create_type=False,
            ),
            nullable=True,
        ),
        sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ['item_id'], ['access_plan_items.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['plan_id'], ['access_plans.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('plan_id', 'item_id'),
    )
    op.create_index(
        'ix_plan_item_executions_plan_id', 'plan_item_executions', ['plan_id'], unique=False
    )

    # ------------------------------------------------------------------
    # access_apply_active — subject-level apply lease
    # ------------------------------------------------------------------
    op.create_table(
        'access_apply_active',
        sa.Column(
            'subject_ref',
            sa.String(length=512),
            nullable=False,
            comment='Opaque subject identifier matching AccessPlan.subject_ref',
        ),
        sa.Column(
            'subject_type',
            sa.String(length=64),
            nullable=False,
            comment='employee | nhi',
        ),
        sa.Column(
            'pipeline_run_id',
            sa.UUID(),
            nullable=False,
            comment='Logical link to platform_runs.id (no FK — Phase 18 schema is in a separate module)',
        ),
        sa.Column('plan_id', sa.UUID(), nullable=False),
        sa.Column(
            'started_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['plan_id'], ['access_plans.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('subject_ref'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('access_apply_active')
    op.drop_index('ix_plan_item_executions_plan_id', table_name='plan_item_executions')
    op.drop_table('plan_item_executions')
    op.drop_index('ix_access_plan_deps_item_id', table_name='access_plan_deps')
    op.drop_table('access_plan_deps')
    op.drop_index('ix_access_plan_items_plan_id', table_name='access_plan_items')
    op.drop_table('access_plan_items')
    op.drop_index('uq_access_plans_idempotency_key', table_name='access_plans')
    op.drop_index('ix_access_plans_supersedes_plan_id', table_name='access_plans')
    op.drop_index('ix_access_plans_subject_ref_active', table_name='access_plans')
    op.drop_table('access_plans')

    # Drop new enum types
    op.execute('DROP TYPE IF EXISTS plan_item_failure_reason')
    op.execute('DROP TYPE IF EXISTS plan_item_execution_status')
    op.execute('DROP TYPE IF EXISTS plan_item_kind')
    op.execute('DROP TYPE IF EXISTS plan_invalidation_reason')
    op.execute('DROP TYPE IF EXISTS access_plan_status')

    # Note: removing an enum value from account_status is not supported in Postgres.
    # 'invited' value added in upgrade() cannot be rolled back via simple ALTER TYPE.
    # Manual step required if downgrade is needed: recreate enum without 'invited'.
