# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityGrant ORM model — projection of EffectiveGrant into business-meaningful capability rows."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base


class CapabilityGrant(Base):
    """Derived projection row: subject S has capability C in scope K=V from grant G via mapping M.

    No partitioning in Phase 13 Step 4 — deferred per phase_13.md.
    No relationship() declarations — cross-slice joins are explicit.
    No event hooks.

    source_effective_grant_id has NO DB-level FK: effective_grants is partitioned with a
    3-column PK (id, subject_kind, application_id). Postgres forbids a unique index on id
    alone on a partitioned parent. Application-side cascade via
    tombstone_capability_grants_for_effective_grant (architect decision: Option A).
    """

    __tablename__ = 'capability_grants'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    subject_id: Mapped[sa.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=False,
    )
    capability_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capabilities.id', ondelete='RESTRICT'),
        nullable=False,
    )
    scope_key_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capability_scope_keys.id', ondelete='RESTRICT'),
        nullable=False,
    )
    # NULL is the GLOBAL sentinel — not a missing value.
    # Active-at predicate: observed_at <= at AND (tombstoned_at IS NULL OR tombstoned_at > at).
    scope_value: Mapped[str | None] = mapped_column(
        sa.String(255),
        nullable=True,
    )
    # Denormalized from source EffectiveGrant.application_id. Required for PER_APPLICATION
    # SoD bucketing without joining EAS on every evaluation. IMMUTABLE post-projection:
    # re-projection MUST NEVER overwrite application_id. Enforced by omitting application_id
    # from the ON CONFLICT DO UPDATE set_ dict in repository.upsert_capability_grants.
    application_id: Mapped[sa.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('applications.id', ondelete='RESTRICT'),
        nullable=False,
    )
    # No DB-level FK — see class docstring for rationale.
    source_effective_grant_id: Mapped[sa.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    source_capability_mapping_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capability_mappings.id', ondelete='RESTRICT'),
        nullable=False,
    )
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    tombstoned_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        # Upsert target.
        sa.UniqueConstraint(
            'source_effective_grant_id',
            'source_capability_mapping_id',
            name='uq_capability_grants_source_pair',
        ),
        # Primary lookup for SoD evaluator (per-subject capability scan).
        sa.Index('ix_capability_grants_subject_capability', 'subject_id', 'capability_id'),
        # Supports BY_SCOPE_KEY rule bucketing.
        sa.Index('ix_capability_grants_capability_scope', 'capability_id', 'scope_key_id', 'scope_value'),
        # Supports PER_APPLICATION rule bucketing.
        sa.Index('ix_capability_grants_subject_application', 'subject_id', 'application_id'),
        # For active filtering.
        sa.Index('ix_capability_grants_tombstoned_at', 'tombstoned_at'),
    )
