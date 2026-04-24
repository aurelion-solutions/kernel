"""Phase 13 Step 6 — SodRule + SodRuleCondition slices.

Creates:
  - sod_severity Postgres enum
  - sod_rule_scope Postgres enum
  - sod_rules table (with CHECK constraints and indexes)
  - sod_rule_conditions table (with CHECK constraint and index)
  - sod_rule_condition_capabilities association table (composite PK, FKs, index)

No seed data — rule codes are customer/regulatory content.

Downgrade reverses strictly:
  1. drop sod_rule_condition_capabilities
  2. drop sod_rule_conditions
  3. drop sod_rules
  4. drop sod_rule_scope enum
  5. drop sod_severity enum
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'e8f9a0b1c2d3'
down_revision = 'd7e8f9a0b1c2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Create sod_severity enum
    sod_severity = postgresql.ENUM(
        'critical', 'high', 'medium', 'low', 'informational',
        name='sod_severity',
        create_type=False,
    )
    sod_severity.create(bind, checkfirst=False)

    # 2. Create sod_rule_scope enum
    sod_rule_scope = postgresql.ENUM(
        'global', 'per_application', 'by_scope_key',
        name='sod_rule_scope',
        create_type=False,
    )
    sod_rule_scope.create(bind, checkfirst=False)

    # 3. Create sod_rules table
    op.create_table(
        'sod_rules',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column(
            'severity',
            postgresql.ENUM(
                'critical', 'high', 'medium', 'low', 'informational',
                name='sod_severity',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            'scope_mode',
            postgresql.ENUM(
                'global', 'per_application', 'by_scope_key',
                name='sod_rule_scope',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('scope_key_id', sa.BigInteger(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('mitigation_allowed', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_sod_rules'),
        sa.UniqueConstraint('code', name='uq_sod_rules_code'),
        sa.ForeignKeyConstraint(
            ['scope_key_id'],
            ['capability_scope_keys.id'],
            name='sod_rules_scope_key_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.CheckConstraint(
            "scope_mode <> 'global' OR scope_key_id IS NULL",
            name='ck_sod_rules_scope_key_global',
        ),
        sa.CheckConstraint(
            "scope_mode <> 'by_scope_key' OR scope_key_id IS NOT NULL",
            name='ck_sod_rules_scope_key_by_scope_key',
        ),
    )
    op.create_index('ix_sod_rules_is_enabled', 'sod_rules', ['is_enabled'])
    op.create_index('ix_sod_rules_scope_mode', 'sod_rules', ['scope_mode'])

    # 4. Create sod_rule_conditions table
    op.create_table(
        'sod_rule_conditions',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('rule_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=True),
        sa.Column('min_count', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint('id', name='pk_sod_rule_conditions'),
        sa.ForeignKeyConstraint(
            ['rule_id'],
            ['sod_rules.id'],
            name='sod_rule_conditions_rule_id_fkey',
            ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            'min_count >= 1',
            name='ck_sod_rule_conditions_min_count_positive',
        ),
    )
    op.create_index('ix_sod_rule_conditions_rule_id', 'sod_rule_conditions', ['rule_id'])

    # 5. Create sod_rule_condition_capabilities association table
    op.create_table(
        'sod_rule_condition_capabilities',
        sa.Column('condition_id', sa.BigInteger(), nullable=False),
        sa.Column('capability_id', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            'condition_id',
            'capability_id',
            name='pk_sod_rule_condition_capabilities',
        ),
        sa.ForeignKeyConstraint(
            ['condition_id'],
            ['sod_rule_conditions.id'],
            name='sod_rule_condition_capabilities_condition_id_fkey',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['capability_id'],
            ['capabilities.id'],
            name='sod_rule_condition_capabilities_capability_id_fkey',
            ondelete='RESTRICT',
        ),
    )
    op.create_index(
        'ix_sod_rule_condition_capabilities_capability_id',
        'sod_rule_condition_capabilities',
        ['capability_id'],
    )


def downgrade() -> None:
    bind = op.get_bind()

    # Reverse order: association → conditions → rules → enums
    op.drop_index(
        'ix_sod_rule_condition_capabilities_capability_id',
        table_name='sod_rule_condition_capabilities',
    )
    op.drop_table('sod_rule_condition_capabilities')

    op.drop_index('ix_sod_rule_conditions_rule_id', table_name='sod_rule_conditions')
    op.drop_table('sod_rule_conditions')

    op.drop_index('ix_sod_rules_scope_mode', table_name='sod_rules')
    op.drop_index('ix_sod_rules_is_enabled', table_name='sod_rules')
    op.drop_table('sod_rules')

    sod_rule_scope = postgresql.ENUM(
        'global', 'per_application', 'by_scope_key',
        name='sod_rule_scope',
        create_type=False,
    )
    sod_rule_scope.drop(bind, checkfirst=False)

    sod_severity = postgresql.ENUM(
        'critical', 'high', 'medium', 'low', 'informational',
        name='sod_severity',
        create_type=False,
    )
    sod_severity.drop(bind, checkfirst=False)
