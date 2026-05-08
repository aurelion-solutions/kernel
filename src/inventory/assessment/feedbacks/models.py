# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Feedback ORM model + FeedbackKind enum.

``feedback_kind`` Postgres enum is OWNED by this step (create_type=True).
Downstream steps that reference it must use ``Enum(..., create_type=False)``.

Feedback rows are immutable: no UPDATE, no DELETE exposed via API.
The CHECK constraint enforces at least one of rule_id / capability_mapping_id / finding_id is set.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class FeedbackKind(StrEnum):
    accepted_risk = 'accepted_risk'
    false_positive = 'false_positive'
    needs_mapping_fix = 'needs_mapping_fix'
    needs_rule_fix = 'needs_rule_fix'
    needs_mitigation = 'needs_mitigation'


# SQLAlchemy Enum type — owned here, create_type=True
_feedback_kind_enum = SaEnum(
    FeedbackKind,
    name='feedback_kind',
    create_type=True,
    values_callable=lambda x: [e.value for e in x],
)


class Feedback(Base):
    """Structured, immutable feedback on SoD findings, capability mappings, and rules.

    No relationship() declarations — cross-slice joins are explicit.
    At least one of rule_id / capability_mapping_id / finding_id MUST be set
    (enforced by DB CHECK and service-level validation).
    """

    __tablename__ = 'feedbacks'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    rule_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('sod_rules.id', ondelete='RESTRICT'),
        nullable=True,
    )
    capability_mapping_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capability_mappings.id', ondelete='RESTRICT'),
        nullable=True,
    )
    finding_id: Mapped[int | None] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('findings.id', ondelete='RESTRICT'),
        nullable=True,
    )
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('subjects.id', ondelete='RESTRICT'),
        nullable=True,
    )
    kind: Mapped[FeedbackKind] = mapped_column(
        _feedback_kind_enum,
        nullable=False,
    )
    message: Mapped[str] = mapped_column(
        sa.Text(),
        nullable=False,
    )
    payload: Mapped[dict | None] = mapped_column(
        JSONB(),
        nullable=True,
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
        # At least one target FK must be set
        sa.CheckConstraint(
            '(rule_id IS NOT NULL) OR (capability_mapping_id IS NOT NULL) OR (finding_id IS NOT NULL)',
            name='ck_feedbacks_target_required',
        ),
        # Filters: kind + recency
        sa.Index('ix_feedbacks_kind_created_at', 'kind', sa.text('created_at DESC')),
        # FK filter indexes
        sa.Index('ix_feedbacks_rule_id', 'rule_id'),
        sa.Index('ix_feedbacks_capability_mapping_id', 'capability_mapping_id'),
        sa.Index('ix_feedbacks_finding_id', 'finding_id'),
    )
