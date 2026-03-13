# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact model — append-only raw JSONB payloads from source systems."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class AccessArtifact(Base):
    """Append-only raw artifact payload ingested from a source system."""

    __tablename__ = 'access_artifacts'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='RESTRICT'),
        nullable=False,
    )
    source_kind: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
    )
    ingested_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    ingest_batch_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    __table_args__ = (
        Index('ix_access_artifacts_application_id', 'application_id'),
        Index('ix_access_artifacts_source_kind', 'source_kind'),
        Index('ix_access_artifacts_ingested_at', sa.desc('ingested_at')),
    )
