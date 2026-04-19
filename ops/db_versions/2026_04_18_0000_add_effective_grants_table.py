"""add effective_grants table (partitioned)

LIST (subject_kind) → HASH (application_id) modulus 4.
Partitioning scheme is load-bearing; changing it later requires a full data migration.

Unique constraint note: spec §3 states uniqueness on (source_access_fact_id, source_initiative_id).
Postgres requires all partition-key columns to be included in every unique constraint on a
partitioned table, so the stored constraint is (source_access_fact_id, source_initiative_id,
subject_kind). This is semantically equivalent: subject_kind is functionally determined by
source_access_fact_id → subject_id → kind.  See §9.5.2 / §9.7 risk 9 in TASK.md.

Revision ID: c9d3e5f7a1b2
Revises: b8c2d4e6f8a9
Create Date: 2026-04-18 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = 'c9d3e5f7a1b2'
down_revision: str | None = 'b8c2d4e6f8a9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create effective_grants partitioned table with child partitions and indexes."""
    # 1. Create new enum type — idempotent
    op.execute(
        """
        DO $$
        BEGIN
            CREATE TYPE effective_grant_effect AS ENUM ('allow', 'deny');
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )

    # 2. Parent partitioned table
    op.execute(
        """
        CREATE TABLE effective_grants (
            id                   uuid          NOT NULL,
            subject_id           uuid          NOT NULL
                REFERENCES subjects(id) ON DELETE RESTRICT,
            subject_kind         subject_kind  NOT NULL,
            application_id       uuid          NOT NULL
                REFERENCES applications(id) ON DELETE RESTRICT,
            account_id           uuid
                REFERENCES ent_accounts(id) ON DELETE SET NULL,
            resource_id          uuid          NOT NULL
                REFERENCES resources(id) ON DELETE RESTRICT,
            action               action        NOT NULL,
            effect               effective_grant_effect NOT NULL,
            initiative_type      initiative_type NOT NULL,
            initiative_origin    varchar(1024) NOT NULL,
            valid_from           timestamptz   NOT NULL,
            valid_until          timestamptz,
            source_access_fact_id uuid         NOT NULL
                REFERENCES access_facts(id) ON DELETE CASCADE,
            source_initiative_id  uuid         NOT NULL
                REFERENCES initiatives(id) ON DELETE CASCADE,
            observed_at          timestamptz   NOT NULL DEFAULT now(),
            tombstoned_at        timestamptz,
            -- application_id included in PK because the LIST sub-partitions
            -- (effective_grants_<kind>) are themselves PARTITION BY HASH (application_id),
            -- and Postgres requires all sub-partition key columns in the PK.
            PRIMARY KEY (id, subject_kind, application_id),
            CONSTRAINT uq_effective_grants_source_pair
                UNIQUE (source_access_fact_id, source_initiative_id, subject_kind, application_id)
        ) PARTITION BY LIST (subject_kind);
        """
    )

    # 3. LIST partitions — one per SubjectKind value
    for kind in ('employee', 'nhi', 'customer'):
        op.execute(
            f"""
            CREATE TABLE effective_grants_{kind}
            PARTITION OF effective_grants
            FOR VALUES IN ('{kind}')
            PARTITION BY HASH (application_id);
            """
        )

    # 4. HASH sub-partitions (3 kinds × 4 buckets = 12 tables)
    for kind in ('employee', 'nhi', 'customer'):
        for remainder in range(4):
            op.execute(
                f"""
                CREATE TABLE effective_grants_{kind}_h{remainder}
                PARTITION OF effective_grants_{kind}
                FOR VALUES WITH (modulus 4, remainder {remainder});
                """
            )

    # 5. DEFAULT partition — safety net; should never receive rows
    op.execute(
        """
        CREATE TABLE effective_grants_default
        PARTITION OF effective_grants DEFAULT;
        """
    )

    # 6. Trap the default partition so any accidental INSERT is rejected
    op.execute(
        """
        ALTER TABLE effective_grants_default
        ADD CONSTRAINT ck_effective_grants_default_trap CHECK (false) NOT VALID;
        """
    )

    # 7. Indexes on parent — Postgres 11+ propagates to all existing and future partitions
    op.create_index(
        'ix_effective_grants_subject_id',
        'effective_grants',
        ['subject_id'],
    )
    op.create_index(
        'ix_effective_grants_resource_id_action_effect',
        'effective_grants',
        ['resource_id', 'action', 'effect'],
    )
    op.create_index(
        'ix_effective_grants_initiative_type_initiative_origin',
        'effective_grants',
        ['initiative_type', 'initiative_origin'],
    )
    op.create_index(
        'ix_effective_grants_tombstoned_at',
        'effective_grants',
        ['tombstoned_at'],
    )


def downgrade() -> None:
    """Drop effective_grants table and the new enum type."""
    op.drop_index('ix_effective_grants_tombstoned_at', table_name='effective_grants')
    op.drop_index(
        'ix_effective_grants_initiative_type_initiative_origin',
        table_name='effective_grants',
    )
    op.drop_index(
        'ix_effective_grants_resource_id_action_effect',
        table_name='effective_grants',
    )
    op.drop_index('ix_effective_grants_subject_id', table_name='effective_grants')

    # CASCADE drops all 16 child partitions (3 LIST + 12 HASH + 1 DEFAULT) atomically
    op.execute('DROP TABLE IF EXISTS effective_grants CASCADE')

    # Drop only the enum type this migration created; reused types are left intact
    op.execute('DROP TYPE IF EXISTS effective_grant_effect')
