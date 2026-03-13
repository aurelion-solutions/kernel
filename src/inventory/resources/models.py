# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resource model — external resource abstraction with privilege/environment/sensitivity metadata."""

from __future__ import annotations

from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from src.core.db.base import Base


class ResourcePrivilegeLevel(StrEnum):
    """Privilege level vocabulary for a resource."""

    admin = 'admin'
    write = 'write'
    read = 'read'
    none = 'none'


class ResourceEnvironment(StrEnum):
    """Environment vocabulary for a resource."""

    production = 'production'
    staging = 'staging'
    dev = 'dev'


class ResourceDataSensitivity(StrEnum):
    """Data sensitivity vocabulary for a resource."""

    pii = 'pii'
    financial = 'financial'
    public = 'public'


class Resource(Base):
    """External resource with privilege/environment/data-sensitivity metadata."""

    __tablename__ = 'resources'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='RESTRICT'),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('resources.id', ondelete='SET NULL'),
        nullable=True,
    )
    path: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
    )
    description: Mapped[str | None] = mapped_column(
        String(1024),
        nullable=True,
    )
    privilege_level: Mapped[ResourcePrivilegeLevel | None] = mapped_column(
        Enum(ResourcePrivilegeLevel, name='resource_privilege_level'),
        nullable=True,
    )
    environment: Mapped[ResourceEnvironment | None] = mapped_column(
        Enum(ResourceEnvironment, name='resource_environment'),
        nullable=True,
    )
    data_sensitivity: Mapped[ResourceDataSensitivity | None] = mapped_column(
        Enum(ResourceDataSensitivity, name='resource_data_sensitivity'),
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

    __table_args__ = (
        UniqueConstraint(
            'application_id',
            'external_id',
            name='uq_resources_application_id_external_id',
        ),
        Index('ix_resources_application_id', 'application_id'),
        Index('ix_resources_kind', 'kind'),
    )

    attributes: Mapped[list[ResourceAttribute]] = relationship(
        'ResourceAttribute',
        back_populates='resource',
        cascade='all, delete-orphan',
    )


class ResourceAttribute(Base):
    """Extensible key/value attribute attached to a Resource."""

    __tablename__ = 'resource_attributes'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('resources.id', ondelete='CASCADE'),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(
        String(1024),
        nullable=False,
    )
    created_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    resource: Mapped[Resource] = relationship(
        'Resource',
        back_populates='attributes',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'resource_id',
            'key',
            name='uq_resource_attributes_resource_id_key',
        ),
    )
