# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Action reference model — controlled vocabulary for normalized access operations."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class Action(Base):
    """Controlled vocabulary entry for a normalized access operation.

    Reference data: write-once by migration, never mutated at runtime.
    New slugs are added via new migrations only.
    """

    __tablename__ = 'ref_actions'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    slug: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
        unique=True,
    )
    description: Mapped[str | None] = mapped_column(
        sa.String(255),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
