# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding model — junction entity linking AccessArtifact to normalized entities."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class ArtifactBinding(Base):
    """Links a raw AccessArtifact to normalized entities (AccessFact, Resource, Account)."""

    __tablename__ = 'artifact_bindings'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('access_artifacts.id', ondelete='CASCADE'),
        nullable=False,
    )
    access_fact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('access_facts.id', ondelete='CASCADE'),
        nullable=True,
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('resources.id', ondelete='CASCADE'),
        nullable=True,
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('ent_accounts.id', ondelete='CASCADE'),
        nullable=True,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            'COALESCE(access_fact_id::text, resource_id::text, account_id::text) IS NOT NULL',
            name='chk_artifact_binding_has_target',
        ),
        Index('ix_artifact_bindings_artifact_id', 'artifact_id'),
        Index('ix_artifact_bindings_access_fact_id', 'access_fact_id'),
        Index('ix_artifact_bindings_resource_id', 'resource_id'),
        Index('ix_artifact_bindings_account_id', 'account_id'),
    )
