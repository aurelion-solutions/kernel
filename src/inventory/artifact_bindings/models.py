# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding model — polymorphic (target_type, target_id) binding to any normalized entity."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class ArtifactBinding(Base):
    """Links a raw AccessArtifact to any normalized entity via a polymorphic (target_type, target_id) pair.

    target_type is an open snake_case string (no DB ENUM); supported values defined in service.py.
    target_id has no DB-level FK — application-level integrity is enforced in the service.
    artifact_id is a soft Iceberg reference (Phase 15 Step 15); integrity is enforced by
    ArtifactBindingService, not by a DB FK.
    """

    __tablename__ = 'artifact_bindings'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    target_type: Mapped[str] = mapped_column(
        sa.String(64),
        nullable=False,
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            'artifact_id',
            'target_type',
            'target_id',
            name='uq_artifact_bindings_artifact_id_target_type_target_id',
        ),
        Index('ix_artifact_bindings_artifact_id', 'artifact_id'),
        Index('ix_artifact_bindings_target', 'target_type', 'target_id'),
    )
