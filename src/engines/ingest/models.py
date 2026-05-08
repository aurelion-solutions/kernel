# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Ingest models: connector result staging.

``staging_connector_results`` is an **operational staging / correlation** store: one row
per ingested connector result envelope (inline JSON or lake_ref location). It is **not**
the path that materializes ``accounts`` / ``roles`` / ``privileges`` — that happens in
**reconciliation** (orchestrator + ``ConnectorClient`` RPC), which reads live connector
data rather than this table.

A separate consumer could read staging rows later for analytics, replay, or a future
normalized pipeline; today the HTTP ingest API only persists here.
"""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base


class StagingConnectorResult(Base):
    """Persisted connector result envelope; does not imply inventory materialization."""

    __tablename__ = 'staging_connector_results'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='CASCADE'),
        nullable=False,
    )
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    result_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
