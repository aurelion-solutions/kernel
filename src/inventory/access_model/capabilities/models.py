# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Capability ORM model — business-action vocabulary for Phase 13 Access Analysis."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class Capability(Base):
    """Business-action vocabulary entry.

    Examples: ``approve_payment``, ``create_vendor``, ``post_journal_entry``.
    Slugs are immutable after creation — SoD rules reference capabilities by slug.
    If a capability must be renamed, deprecate the old slug and create a new one.
    """

    __tablename__ = 'capabilities'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    slug: Mapped[str] = mapped_column(
        sa.String(128),
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
        sa.UniqueConstraint('slug', name='uq_capabilities_slug'),
        sa.Index('ix_capabilities_is_active', 'is_active'),
    )
