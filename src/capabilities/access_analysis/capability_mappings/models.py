# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityMapping ORM model — matcher rules that translate EffectiveGrants into CapabilityGrants."""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class CapabilityMapping(Base):
    """Rule that maps a raw EffectiveGrant to a business-meaningful CapabilityGrant.

    resource_match is three nullable columns with a CHECK constraint that enforces
    exactly one is non-null (XOR semantics). This makes each column queryable,
    indexable, and FK-able — properties JSONB would lose.

    scope_value_source is JSONB holding a discriminated-union of four kinds:
    subject_attribute, resource_attribute, application_id, constant.
    """

    __tablename__ = 'capability_mappings'

    id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        primary_key=True,
        autoincrement=True,
    )
    capability_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capabilities.id', ondelete='RESTRICT'),
        nullable=False,
    )
    application_id: Mapped[sa.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('applications.id', ondelete='RESTRICT'),
        nullable=True,
    )
    resource_id: Mapped[sa.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('resources.id', ondelete='RESTRICT'),
        nullable=True,
    )
    resource_kind: Mapped[str | None] = mapped_column(
        sa.String(128),
        nullable=True,
    )
    resource_path_glob: Mapped[str | None] = mapped_column(
        sa.String(512),
        nullable=True,
    )
    action_slug: Mapped[str | None] = mapped_column(
        sa.String(64),
        nullable=True,
    )
    scope_key_id: Mapped[int] = mapped_column(
        sa.BigInteger(),
        sa.ForeignKey('capability_scope_keys.id', ondelete='RESTRICT'),
        nullable=False,
    )
    scope_value_source: Mapped[dict] = mapped_column(
        JSONB(),
        nullable=False,
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
        sa.CheckConstraint(
            'num_nonnulls(resource_id, resource_kind, resource_path_glob) = 1',
            name='ck_capability_mappings_resource_match_xor',
        ),
        sa.Index('ix_capability_mappings_capability_id', 'capability_id'),
        sa.Index('ix_capability_mappings_application_id', 'application_id'),
        sa.Index('ix_capability_mappings_resource_id', 'resource_id'),
        sa.Index('ix_capability_mappings_scope_key_id', 'scope_key_id'),
        sa.Index('ix_capability_mappings_is_active', 'is_active'),
    )
