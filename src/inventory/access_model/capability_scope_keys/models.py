# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityScopeKey ORM model — scope vocabulary for Phase 13 Access Analysis."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class CapabilityScopeKey(Base):
    """Scope vocabulary entry for Capability exercises.

    Examples: ``GLOBAL``, ``LEGAL_ENTITY``, ``COST_CENTER``, ``PROJECT``.
    Codes are immutable after creation — mappings and rules reference them by id,
    but the code is the human-stable identifier. Renaming silently breaks operator scripts.
    """

    __tablename__ = 'capability_scope_keys'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    code: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        default=True,
        server_default=sa.text('true'),
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
        sa.UniqueConstraint('code', name='uq_capability_scope_keys_code'),
        sa.Index('ix_capability_scope_keys_is_active', 'is_active'),
    )
