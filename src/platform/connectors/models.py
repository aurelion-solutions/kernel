# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from datetime import UTC, datetime, timedelta
import uuid

import sqlalchemy as sa
from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base


class ConnectorInstance(Base):
    __tablename__ = 'connector_instances'

    __table_args__ = (UniqueConstraint('instance_id', name='uq_connector_instances_instance_id'),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    instance_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    descriptor: Mapped[dict[str, object] | None] = mapped_column(
        JSONB,
        nullable=True,
        default=None,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    @property
    def is_online(self) -> bool:
        now = datetime.now(UTC)
        return self.last_seen_at >= now - timedelta(minutes=2)
