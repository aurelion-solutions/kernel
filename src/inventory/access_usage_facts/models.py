# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact model — records observed usage telemetry for a normalized AccessFact."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class AccessUsageFact(Base):
    """Telemetry window: how often an AccessFact was exercised and when."""

    __tablename__ = 'access_usage_facts'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    access_fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    last_seen: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    usage_count: Mapped[int] = mapped_column(
        sa.Integer,
        nullable=False,
        default=0,
    )
    window_from: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    window_to: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint('usage_count >= 0', name='chk_access_usage_facts_usage_count_nonneg'),
        CheckConstraint(
            'window_to IS NULL OR window_to > window_from',
            name='chk_access_usage_facts_window_ordering',
        ),
        UniqueConstraint(
            'access_fact_id',
            'window_from',
            'window_to',
            name='uq_access_usage_facts_fact_window',
            postgresql_nulls_not_distinct=True,
        ),
        Index('ix_access_usage_facts_access_fact_id', 'access_fact_id'),
        Index('ix_access_usage_facts_last_seen', 'last_seen'),
        Index('ix_access_usage_facts_window', 'window_from', 'window_to'),
    )
