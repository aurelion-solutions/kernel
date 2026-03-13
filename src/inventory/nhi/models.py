# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""NHI and NHIAttribute models."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.db.base import Base
from src.inventory.employees.models import Employee
from src.platform.applications.models import Application

NHI_KIND_SERVICE_ACCOUNT = 'service_account'
NHI_KIND_BOT = 'bot'
NHI_KIND_API_CLIENT = 'api_client'
NHI_KIND_MACHINE_IDENTITY = 'machine_identity'
NHI_KIND_WORKLOAD = 'workload'


class NHI(Base):
    """Canonical non-human principal."""

    __tablename__ = 'nhis'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_locked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sa.text('false'),
    )
    owner_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('employees.id', ondelete='SET NULL'),
        nullable=True,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='SET NULL'),
        nullable=True,
    )

    owner_employee: Mapped[Employee | None] = relationship(
        'Employee',
        foreign_keys=[owner_employee_id],
    )
    application: Mapped[Application | None] = relationship(
        'Application',
        foreign_keys=[application_id],
    )
    attributes: Mapped[list[NHIAttribute]] = relationship(
        'NHIAttribute',
        back_populates='nhi',
        cascade='all, delete-orphan',
    )


class NHIAttribute(Base):
    """Extensible attribute attached to an NHI."""

    __tablename__ = 'nhi_attributes'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    nhi_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('nhis.id', ondelete='CASCADE'),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(String(1024), nullable=False)

    nhi: Mapped[NHI] = relationship('NHI', back_populates='attributes')

    __table_args__ = (
        sa.UniqueConstraint(
            'nhi_id',
            'key',
            name='uq_nhi_attributes_nhi_id_key',
        ),
    )
