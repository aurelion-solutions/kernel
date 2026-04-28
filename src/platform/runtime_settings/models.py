# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SQLAlchemy ORM model for runtime_settings table."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base


class RuntimeSetting(Base):
    """Operator-tunable knob stored in PostgreSQL.

    key        — unique string identifier (e.g. ``lake_pool_size``)
    value      — serialized string value (callers own serialization)
    value_type — informational hint (``int``, ``float``, ``str``, etc.)
    updated_at — last-write timestamp (UTC)
    """

    __tablename__ = 'runtime_settings'

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String, nullable=False)
    value_type: Mapped[str] = mapped_column(String, nullable=False, default='str')
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
    )
