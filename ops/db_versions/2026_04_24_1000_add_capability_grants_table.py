"""Phase 13 Step 4 — CapabilityGrant projection table.

FKs to subjects, capabilities, capability_scope_keys, applications,
effective_grants (CASCADE), capability_mappings (RESTRICT). Partitioning
deferred per phase_13.md — bounded by realistic IGA scale.

Note on source_effective_grant_id FK:
  No DB-level FK is created for source_effective_grant_id. The effective_grants
  table is partitioned with a 3-column PK (id, subject_kind, application_id).
  Postgres requires a UNIQUE index on the referenced column(s); creating one on
  id alone on a partitioned parent is forbidden (each partition has its own index,
  but the parent does not have a standalone unique index on id). Option B (denormalizing
  subject_kind into capability_grants) was rejected because it leaks EAS partition
  semantics into the projection layer. Architect decision: Option A — no DB FK;
  cascade is application-side via tombstone_capability_grants_for_effective_grant.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd7e8f9a0b1c2'
down_revision = 'c6d7e8f9a0b1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'capability_grants',
        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('subject_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('capability_id', sa.BigInteger(), nullable=False),
        sa.Column('scope_key_id', sa.BigInteger(), nullable=False),
        sa.Column('scope_value', sa.String(length=255), nullable=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source_effective_grant_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('source_capability_mapping_id', sa.BigInteger(), nullable=False),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('tombstoned_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id', name='pk_capability_grants'),
        sa.ForeignKeyConstraint(
            ['subject_id'],
            ['subjects.id'],
            name='capability_grants_subject_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['capability_id'],
            ['capabilities.id'],
            name='capability_grants_capability_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scope_key_id'],
            ['capability_scope_keys.id'],
            name='capability_grants_scope_key_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['application_id'],
            ['applications.id'],
            name='capability_grants_application_id_fkey',
            ondelete='RESTRICT',
        ),
        # No FK for source_effective_grant_id: effective_grants is partitioned with
        # 3-column PK (id, subject_kind, application_id); Postgres forbids a unique
        # index on id alone on the partitioned parent. Application-side cascade via
        # tombstone_capability_grants_for_effective_grant (architect decision: Option A).
        sa.ForeignKeyConstraint(
            ['source_capability_mapping_id'],
            ['capability_mappings.id'],
            name='capability_grants_source_capability_mapping_id_fkey',
            ondelete='RESTRICT',
        ),
        sa.UniqueConstraint(
            'source_effective_grant_id',
            'source_capability_mapping_id',
            name='uq_capability_grants_source_pair',
        ),
    )
    op.create_index(
        'ix_capability_grants_subject_capability',
        'capability_grants',
        ['subject_id', 'capability_id'],
    )
    op.create_index(
        'ix_capability_grants_capability_scope',
        'capability_grants',
        ['capability_id', 'scope_key_id', 'scope_value'],
    )
    op.create_index(
        'ix_capability_grants_subject_application',
        'capability_grants',
        ['subject_id', 'application_id'],
    )
    op.create_index(
        'ix_capability_grants_tombstoned_at',
        'capability_grants',
        ['tombstoned_at'],
    )


def downgrade() -> None:
    op.drop_index('ix_capability_grants_tombstoned_at', table_name='capability_grants')
    op.drop_index('ix_capability_grants_subject_application', table_name='capability_grants')
    op.drop_index('ix_capability_grants_capability_scope', table_name='capability_grants')
    op.drop_index('ix_capability_grants_subject_capability', table_name='capability_grants')
    op.drop_table('capability_grants')
