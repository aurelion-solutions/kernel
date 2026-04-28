# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EffectiveGrant model — projection row of the Effective Access Store (Phase 09)."""

from __future__ import annotations

from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import DDL, Enum, ForeignKey, Index, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind


class EffectiveGrantEffect(StrEnum):
    """Effect of an effective grant: allow or deny.

    Intentionally separate from AccessFactEffect so EAS can evolve its vocabulary
    independently without altering the inventory-owned PG enum type.
    """

    allow = 'allow'
    deny = 'deny'


class EffectiveGrant(Base):
    """Projection row — one (subject, application, entitlement, initiative) tuple."""

    __tablename__ = 'effective_grants'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=False,
    )
    # Partition key — must be part of PK per Postgres partitioning constraint.
    subject_kind: Mapped[SubjectKind] = mapped_column(
        Enum(SubjectKind, name='subject_kind', create_type=False),
        primary_key=True,
        nullable=False,
    )
    # HASH sub-partition key — must be part of PK because effective_grants_<kind> is
    # PARTITION BY HASH (application_id), and Postgres requires partition key columns
    # in the PK of every sub-partitioned table.
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='RESTRICT'),
        primary_key=True,
        nullable=False,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('ent_accounts.id', ondelete='SET NULL'),
        nullable=True,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('resources.id', ondelete='RESTRICT'),
        nullable=False,
    )
    action: Mapped[Action] = mapped_column(
        Enum(Action, name='action', create_type=False),
        nullable=False,
    )
    effect: Mapped[EffectiveGrantEffect] = mapped_column(
        Enum(EffectiveGrantEffect, name='effective_grant_effect', create_type=False),
        nullable=False,
    )
    initiative_type: Mapped[InitiativeType] = mapped_column(
        Enum(InitiativeType, name='initiative_type', create_type=False),
        nullable=False,
    )
    initiative_origin: Mapped[str] = mapped_column(
        sa.String(1024),
        nullable=False,
    )
    valid_from: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        # No server_default — projector must always supply this value explicitly.
        # A default here would mask projector bugs in Step 2.
    )
    valid_until: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    source_access_fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    source_initiative_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('initiatives.id', ondelete='CASCADE'),
        nullable=False,
    )
    observed_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    tombstoned_at: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        # 4-column unique: Postgres requires ALL partition-key columns in every UNIQUE constraint
        # on a partitioned table (and its sub-partitioned children).
        # Columns (subject_kind, application_id) are the full partition key path.
        # Logically the constraint is (source_access_fact_id, source_initiative_id) — the extras
        # are structurally determined by those two columns via the source rows.
        # See §9.5.2 and §9.7 risk 9 in TASK.md for rationale.
        UniqueConstraint(
            'source_access_fact_id',
            'source_initiative_id',
            'subject_kind',
            'application_id',
            name='uq_effective_grants_source_pair',
        ),
        Index('ix_effective_grants_subject_id', 'subject_id'),
        Index('ix_effective_grants_resource_id_action_effect', 'resource_id', 'action', 'effect'),
        Index(
            'ix_effective_grants_initiative_type_initiative_origin',
            'initiative_type',
            'initiative_origin',
        ),
        # Mirrors Alembic migration d1a4f7b9c3e5 so Base.metadata.create_all produces
        # the same index set as migrated environments. Must remain BEFORE the
        # postgresql_partition_by dialect-options dict (SQLAlchemy requires that dict
        # as the final tuple element).
        Index('ix_effective_grants_source_initiative_id', 'source_initiative_id'),
        Index('ix_effective_grants_tombstoned_at', 'tombstoned_at'),
        {'postgresql_partition_by': 'LIST (subject_kind)'},
    )


# ---------------------------------------------------------------------------
# DDL event listeners — required for Base.metadata.create_all() parity with
# Alembic migrations in the test fixture.  Without these, create_all emits
# the partitioned parent table but no child partitions, and every INSERT
# fails with "no partition of relation found for row".
# ---------------------------------------------------------------------------

_CREATE_ENUM_DDL = DDL(
    """
    DO $$
    BEGIN
        CREATE TYPE effective_grant_effect AS ENUM ('allow', 'deny');
    EXCEPTION WHEN duplicate_object THEN NULL;
    END $$;
    """
)

_PARTITION_STATEMENTS: tuple[str, ...] = (
    # LIST partitions — one per SubjectKind value
    'CREATE TABLE IF NOT EXISTS effective_grants_employee '
    "PARTITION OF effective_grants FOR VALUES IN ('employee') "
    'PARTITION BY HASH (application_id);',
    'CREATE TABLE IF NOT EXISTS effective_grants_nhi '
    "PARTITION OF effective_grants FOR VALUES IN ('nhi') "
    'PARTITION BY HASH (application_id);',
    'CREATE TABLE IF NOT EXISTS effective_grants_customer '
    "PARTITION OF effective_grants FOR VALUES IN ('customer') "
    'PARTITION BY HASH (application_id);',
    # HASH sub-partitions for employee
    'CREATE TABLE IF NOT EXISTS effective_grants_employee_h0 '
    'PARTITION OF effective_grants_employee FOR VALUES WITH (modulus 4, remainder 0);',
    'CREATE TABLE IF NOT EXISTS effective_grants_employee_h1 '
    'PARTITION OF effective_grants_employee FOR VALUES WITH (modulus 4, remainder 1);',
    'CREATE TABLE IF NOT EXISTS effective_grants_employee_h2 '
    'PARTITION OF effective_grants_employee FOR VALUES WITH (modulus 4, remainder 2);',
    'CREATE TABLE IF NOT EXISTS effective_grants_employee_h3 '
    'PARTITION OF effective_grants_employee FOR VALUES WITH (modulus 4, remainder 3);',
    # HASH sub-partitions for nhi
    'CREATE TABLE IF NOT EXISTS effective_grants_nhi_h0 '
    'PARTITION OF effective_grants_nhi FOR VALUES WITH (modulus 4, remainder 0);',
    'CREATE TABLE IF NOT EXISTS effective_grants_nhi_h1 '
    'PARTITION OF effective_grants_nhi FOR VALUES WITH (modulus 4, remainder 1);',
    'CREATE TABLE IF NOT EXISTS effective_grants_nhi_h2 '
    'PARTITION OF effective_grants_nhi FOR VALUES WITH (modulus 4, remainder 2);',
    'CREATE TABLE IF NOT EXISTS effective_grants_nhi_h3 '
    'PARTITION OF effective_grants_nhi FOR VALUES WITH (modulus 4, remainder 3);',
    # HASH sub-partitions for customer
    'CREATE TABLE IF NOT EXISTS effective_grants_customer_h0 '
    'PARTITION OF effective_grants_customer FOR VALUES WITH (modulus 4, remainder 0);',
    'CREATE TABLE IF NOT EXISTS effective_grants_customer_h1 '
    'PARTITION OF effective_grants_customer FOR VALUES WITH (modulus 4, remainder 1);',
    'CREATE TABLE IF NOT EXISTS effective_grants_customer_h2 '
    'PARTITION OF effective_grants_customer FOR VALUES WITH (modulus 4, remainder 2);',
    'CREATE TABLE IF NOT EXISTS effective_grants_customer_h3 '
    'PARTITION OF effective_grants_customer FOR VALUES WITH (modulus 4, remainder 3);',
    # DEFAULT partition — safety net only; trapped by CHECK (false) NOT VALID
    'CREATE TABLE IF NOT EXISTS effective_grants_default PARTITION OF effective_grants DEFAULT;',
    'ALTER TABLE effective_grants_default ADD CONSTRAINT ck_effective_grants_default_trap CHECK (false) NOT VALID;',
)


def _create_partitions(target, connection, **kw):  # type: ignore[no-untyped-def]
    for stmt in _PARTITION_STATEMENTS:
        connection.execute(sa.text(stmt))


event.listen(EffectiveGrant.__table__, 'before_create', _CREATE_ENUM_DDL)
event.listen(EffectiveGrant.__table__, 'after_create', _create_partitions)
