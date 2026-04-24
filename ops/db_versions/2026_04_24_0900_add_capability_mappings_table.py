"""Phase 13 Step 3 — CapabilityMapping slice (schema only).

FKs to capabilities, applications, resources, capability_scope_keys;
CHECK enforces resource-match XOR. No seed data — mappings are operator-defined per environment.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = 'c6d7e8f9a0b1'
down_revision = 'b5c6d7e8f9a0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'capability_mappings',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('capability_id', sa.BigInteger(), nullable=False),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('resource_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('resource_kind', sa.String(length=128), nullable=True),
        sa.Column('resource_path_glob', sa.String(length=512), nullable=True),
        sa.Column('action_slug', sa.String(length=64), nullable=True),
        sa.Column('scope_key_id', sa.BigInteger(), nullable=False),
        sa.Column('scope_value_source', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_capability_mappings'),
        sa.ForeignKeyConstraint(
            ['capability_id'],
            ['capabilities.id'],
            name='capability_mappings_capability_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['application_id'],
            ['applications.id'],
            name='capability_mappings_application_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['resource_id'],
            ['resources.id'],
            name='capability_mappings_resource_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scope_key_id'],
            ['capability_scope_keys.id'],
            name='capability_mappings_scope_key_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.CheckConstraint(
            'num_nonnulls(resource_id, resource_kind, resource_path_glob) = 1',
            name='ck_capability_mappings_resource_match_xor',
        ),
    )
    op.create_index('ix_capability_mappings_capability_id', 'capability_mappings', ['capability_id'])
    op.create_index('ix_capability_mappings_application_id', 'capability_mappings', ['application_id'])
    op.create_index('ix_capability_mappings_resource_id', 'capability_mappings', ['resource_id'])
    op.create_index('ix_capability_mappings_scope_key_id', 'capability_mappings', ['scope_key_id'])
    op.create_index('ix_capability_mappings_is_active', 'capability_mappings', ['is_active'])


def downgrade() -> None:
    op.drop_index('ix_capability_mappings_is_active', table_name='capability_mappings')
    op.drop_index('ix_capability_mappings_scope_key_id', table_name='capability_mappings')
    op.drop_index('ix_capability_mappings_resource_id', table_name='capability_mappings')
    op.drop_index('ix_capability_mappings_application_id', table_name='capability_mappings')
    op.drop_index('ix_capability_mappings_capability_id', table_name='capability_mappings')
    op.drop_table('capability_mappings')
