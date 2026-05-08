# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Mitigation ORM model + MitigationStatus enum.

``mitigation_status`` Postgres enum is OWNED by this step (create_type=True).
Downstream steps that reference it must use ``Enum(..., create_type=False)``.

Partial unique index ``uq_mitigations_active_or_proposed`` uses NULLS NOT DISTINCT
(Postgres 15+, project runs PG17) so that two unscoped mitigations
(scope_key_id IS NULL, scope_value IS NULL) for the same (rule_id, subject_id) pair
cannot both be in 'active' or 'proposed' status simultaneously.  Without NULLS NOT
DISTINCT, Postgres would treat the two NULL scope columns as distinct — allowing
duplicate unscoped rows — which violates the invariant.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class MitigationStatus(StrEnum):
    proposed = 'proposed'
    active = 'active'
    expired = 'expired'
    revoked = 'revoked'


# SQLAlchemy Enum type — owned here, create_type=True
_mitigation_status_enum = SaEnum(
    MitigationStatus,
    name='mitigation_status',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


class Mitigation(Base):
    """Per-subject, per-rule, time-bound mitigation record.

    No relationship() declarations — cross-slice joins are explicit.
    Findings FK to this table is added by the Step 9 migration as a deferred FK
    (plain BigInteger columns were added by Step 7; FK constraint added here).
    """

    __tablename__ = 'mitigations'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    rule_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('sod_rules.id', ondelete='RESTRICT'),
        nullable=False,
    )
    control_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('mitigation_controls.id', ondelete='RESTRICT'),
        nullable=False,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=False,
    )
    scope_key_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capability_scope_keys.id', ondelete='RESTRICT'),
        nullable=True,
    )
    scope_value: Mapped[str | None] = mapped_column(
        sa.String(255),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    status: Mapped[MitigationStatus] = mapped_column(
        _mitigation_status_enum,
        nullable=False,
        default=MitigationStatus.proposed,
        server_default='proposed',
    )
    valid_from: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_by: Mapped[str | None] = mapped_column(
        sa.String(255),
        nullable=True,
    )

    __table_args__ = (
        # Scope pair invariant: both set or both null
        sa.CheckConstraint(
            '(scope_key_id IS NULL) = (scope_value IS NULL)',
            name='ck_mitigations_scope_pair',
        ),
        # Valid window: valid_until must be strictly after valid_from if set
        sa.CheckConstraint(
            'valid_until IS NULL OR valid_until > valid_from',
            name='ck_mitigations_valid_window',
        ),
        # Partial unique: only one active or proposed per (rule, subject, scope) tuple.
        # NULLS NOT DISTINCT (PG15+) ensures two unscoped rows can't both be active/proposed.
        # See module docstring for rationale.
        sa.Index(
            'uq_mitigations_active_or_proposed',
            'rule_id',
            'subject_id',
            'scope_key_id',
            'scope_value',
            unique=True,
            postgresql_where=sa.text("status IN ('active', 'proposed')"),
            postgresql_nulls_not_distinct=True,
        ),
        # Evaluator primary lookup: find active/proposed mitigations for a subject+rule
        sa.Index(
            'ix_mitigations_subject_rule_status',
            'subject_id',
            'rule_id',
            'status',
            'valid_from',
            'valid_until',
        ),
        # Expiry sweep candidate scan
        sa.Index('ix_mitigations_valid_until', 'valid_until'),
        # Usage inspection when admins inspect a control
        sa.Index('ix_mitigations_control_id', 'control_id'),
        # Owner dashboards
        sa.Index('ix_mitigations_owner_id', 'owner_id'),
    )
