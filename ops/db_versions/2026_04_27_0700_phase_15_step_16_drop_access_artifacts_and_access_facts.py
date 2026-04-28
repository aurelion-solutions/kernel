"""Phase 15 Step 16 — DROP TABLE access_artifacts and access_facts.

What:
    Drops both PostgreSQL tables ``access_artifacts`` and ``access_facts``.
    All associated indexes, partial unique constraints, and the ``access_fact_effect``
    PostgreSQL enum are removed.  Iceberg (``raw.access_artifacts``,
    ``normalized.access_facts``) is now the sole storage backend for both datasets.

Why:
    Step 15 removed the FK artifact_bindings → access_artifacts.
    Revision ``j4k5l6m7n8o9`` (Step 16b) removed the remaining inbound FKs
    (effective_grants, access_usage_facts, initiatives → access_facts).
    With no inbound FK constraints remaining, DROP TABLE can now proceed.
    The PG tables are no longer written to (Iceberg write path active since Step 5/12).
    Removing them eliminates all ambiguity about the source of truth.

Downgrade (schema-only — data restoration NOT provided):
    Recreates both tables as they existed at the end of Step 15 (FK from
    artifact_bindings already absent).  No data is restored on downgrade.
    This is an intentional trade-off documented here.

Constraint note:
    ``access_fact_effect`` PG enum is dropped via ``sa.Enum(...).drop(bind, checkfirst=True)``.
    The enum must be dropped after the table that owns it.
"""

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = 'i3j4k5l6m7n8'
down_revision: str = 'j4k5l6m7n8o9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Drop access_facts, then access_artifacts, then the access_fact_effect enum."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = inspector.get_table_names()

    if 'access_facts' in existing_tables:
        op.drop_table('access_facts')

    if 'access_artifacts' in existing_tables:
        op.drop_table('access_artifacts')

    # Drop the PG enum created by SQLAlchemy for AccessFactEffect
    access_fact_effect_enum = sa.Enum('allow', 'deny', name='access_fact_effect')
    access_fact_effect_enum.drop(bind, checkfirst=True)


def downgrade() -> None:
    """Recreate both tables (schema-only — data restoration NOT provided).

    This downgrade recreates the table schemas as they existed at the end of Step 15.
    The FK from artifact_bindings.artifact_id to access_artifacts.id was removed in
    Step 15 and is NOT recreated here (Step 15 downgrade handles that).
    """
    bind = op.get_bind()

    # Recreate access_fact_effect enum first (needed by access_facts table)
    access_fact_effect_enum = sa.Enum('allow', 'deny', name='access_fact_effect')
    access_fact_effect_enum.create(bind, checkfirst=True)

    # Recreate access_artifacts
    op.create_table(
        'access_artifacts',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('application_id', UUID(as_uuid=True), sa.ForeignKey('applications.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('artifact_type', sa.String(255), nullable=False),
        sa.Column('external_id', sa.String(255), nullable=False),
        sa.Column('payload', JSONB, nullable=False),
        sa.Column('ingested_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('ingest_batch_id', sa.String(255), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('raw_name', sa.String(255), nullable=True),
        sa.Column('effect', sa.Text, nullable=True),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=True),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column('tombstoned_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            'application_id', 'artifact_type', 'external_id',
            name='uq_access_artifacts_application_id_artifact_type_external_id',
        ),
    )
    op.create_index('ix_access_artifacts_application_id', 'access_artifacts', ['application_id'])
    op.create_index('ix_access_artifacts_artifact_type', 'access_artifacts', ['artifact_type'])
    op.create_index('ix_access_artifacts_ingested_at', 'access_artifacts', [sa.text('ingested_at DESC')])

    # Recreate access_facts
    op.create_table(
        'access_facts',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('subject_id', UUID(as_uuid=True), sa.ForeignKey('subjects.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('account_id', UUID(as_uuid=True), sa.ForeignKey('ent_accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('resource_id', UUID(as_uuid=True), sa.ForeignKey('resources.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('action_id', sa.BigInteger(), sa.ForeignKey('ref_actions.id', ondelete='RESTRICT'), nullable=False),
        sa.Column('effect', sa.Enum('allow', 'deny', name='access_fact_effect', create_type=False), nullable=False),
        sa.Column('valid_from', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column('valid_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('observed_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_access_facts_subject_id', 'access_facts', ['subject_id'])
    op.create_index('ix_access_facts_resource_id', 'access_facts', ['resource_id'])
    op.create_index('ix_access_facts_account_id', 'access_facts', ['account_id'])
    op.create_index('ix_access_facts_action_id', 'access_facts', ['action_id'])
    op.create_index('ix_access_facts_valid_window', 'access_facts', ['valid_from', 'valid_until'])
    op.create_index(
        'ix_access_facts_is_active', 'access_facts', ['is_active'],
        postgresql_where=sa.text('is_active = false'),
    )
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
