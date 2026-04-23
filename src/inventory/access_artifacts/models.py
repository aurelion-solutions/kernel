# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact model — raw JSONB payloads from source systems with lifecycle tracking."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class AccessArtifact(Base):
    """Raw artifact payload from a source system with lifecycle columns."""

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
    artifact_type: Mapped[str] = mapped_column(
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
    observed_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
    )
    raw_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    # Source-raw effect string, nullable, no normalization.
    # Distinct from `AccessFact.effect` which is the normalized `allow | deny` vocabulary (Phase 12 Step 13).
    # Handlers read this raw value and map it when projecting to an `AccessFact`.
    effect: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    valid_from: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    valid_until: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.true(),
    )
    tombstoned_at: Mapped[sa.DateTime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index('ix_access_artifacts_application_id', 'application_id'),
        Index('ix_access_artifacts_artifact_type', 'artifact_type'),
        Index('ix_access_artifacts_ingested_at', sa.desc('ingested_at')),
        UniqueConstraint(
            'application_id',
            'artifact_type',
            'external_id',
            name='uq_access_artifacts_application_id_artifact_type_external_id',
        ),
    )
