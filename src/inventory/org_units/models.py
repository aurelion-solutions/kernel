# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit model."""

import uuid

import sqlalchemy as sa
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class OrgUnit(Base):
    """Organisational unit (department, team, etc.)."""

    __tablename__ = 'org_units'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )
    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('org_units.id', ondelete='SET NULL'),
        nullable=True,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (sa.UniqueConstraint('external_id', name='uq_org_units_external_id'),)
