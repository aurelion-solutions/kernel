# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ThreatFact model — carries risk-signal input for the PDP."""

from __future__ import annotations

from datetime import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func, text
from src.core.db.base import Base


class ThreatFact(Base):
    """One-row-per-subject risk snapshot: risk score, active threat indicators, login metadata."""

    __tablename__ = 'threat_facts'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('subjects.id', ondelete='CASCADE'),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('ent_accounts.id', ondelete='SET NULL'),
        nullable=True,
    )
    risk_score: Mapped[float] = mapped_column(
        sa.Float,
        nullable=False,
    )
    active_indicators: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(255)),
        nullable=False,
        server_default=text("'{}'::text[]"),
        default=list,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    failed_auth_count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
        server_default='0',
    )
    observed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            'risk_score >= 0.0 AND risk_score <= 1.0',
            name='chk_threat_facts_risk_score_range',
        ),
        CheckConstraint(
            'failed_auth_count >= 0',
            name='chk_threat_facts_failed_auth_count_nonneg',
        ),
        UniqueConstraint('subject_id', name='uq_threat_facts_subject_id'),
        Index('ix_threat_facts_account_id', 'account_id'),
        Index('ix_threat_facts_risk_score', 'risk_score'),
        Index('ix_threat_facts_observed_at', 'observed_at'),
    )
