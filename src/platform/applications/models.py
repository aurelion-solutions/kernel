# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from src.platform.connectors.models import ConnectorInstance


class Application(Base):
    __tablename__ = 'applications'

    __table_args__ = (
        UniqueConstraint('name', name='uq_applications_name'),
        UniqueConstraint('code', name='uq_applications_code'),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
    )
    config: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
    )
    required_connector_tags: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=sa.true(),
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

    async def matching_connector_instances(
        self,
        session: AsyncSession,
        *,
        online_only: bool = True,
    ) -> list[ConnectorInstance]:
        """Connector instances whose tags satisfy ``required_connector_tags``.

        When ``online_only`` is True, only instances with a recent ``last_seen_at``
        are considered (same pool shape as provisioning / reconciliation).

        Does not run stale-row cleanup (see ``ConnectorInstanceService.cleanup_stale_instances``).
        """
        from src.platform.connectors.repository import (
            list_connector_instances,
            list_online_connector_instances,
        )
        from src.platform.connectors.selector import list_connector_instances_matching_tags

        if online_only:
            pool = await list_online_connector_instances(session)
        else:
            pool = await list_connector_instances(session)
        return list_connector_instances_matching_tags(
            pool,
            list(self.required_connector_tags or []),
        )
