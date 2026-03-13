# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative model — entity recording why an AccessFact exists."""

from __future__ import annotations

import enum
import uuid

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class InitiativeType(str, enum.Enum):
    """Closed 9-value vocabulary for initiative types. Source of truth for all layers."""

    birthright = 'birthright'
    requested = 'requested'
    delegated = 'delegated'
    inherited = 'inherited'
    grace = 'grace'
    self_registered = 'self_registered'
    invited = 'invited'
    trial = 'trial'
    subscription = 'subscription'


class Initiative(Base):
    """Records why an AccessFact exists. N:1 to AccessFact."""

    __tablename__ = 'initiatives'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    access_fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('access_facts.id', ondelete='CASCADE'),
        nullable=False,
    )
    type: Mapped[InitiativeType] = mapped_column(
        sa.Enum(InitiativeType, name='initiative_type', create_type=False),
        nullable=False,
    )
    origin: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    valid_from: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    valid_until: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        Index('ix_initiatives_access_fact_id', 'access_fact_id'),
        Index('ix_initiatives_type', 'type'),
        Index('ix_initiatives_valid_window', 'valid_from', 'valid_until'),
    )
