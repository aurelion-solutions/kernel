# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmployeeRecord and EmployeeRecordAttribute models."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.db.base import Base
from src.platform.applications.models import Application

if TYPE_CHECKING:
    from src.inventory.employees.models import Employee


class EmployeeRecord(Base):
    """External source-side human record. Belongs to Application."""

    __tablename__ = 'employee_records'

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
        ForeignKey('applications.id', ondelete='CASCADE'),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    application: Mapped[Application] = relationship(
        'Application',
    )
    attributes: Mapped[list[EmployeeRecordAttribute]] = relationship(
        'EmployeeRecordAttribute',
        back_populates='employee_record',
        cascade='all, delete-orphan',
    )
    match: Mapped[EmployeeRecordMatch | None] = relationship(
        'EmployeeRecordMatch',
        back_populates='employee_record',
        uselist=False,
    )


class EmployeeRecordAttribute(Base):
    """Extensible attribute attached to an EmployeeRecord."""

    __tablename__ = 'employee_record_attributes'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    employee_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('employee_records.id', ondelete='CASCADE'),
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

    employee_record: Mapped[EmployeeRecord] = relationship(
        'EmployeeRecord',
        back_populates='attributes',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'employee_record_id',
            'key',
            name='uq_employee_record_attributes_employee_record_id_key',
        ),
    )


class EmployeeProviderAttributeMapping(Base):
    """Maps source EmployeeRecordAttribute keys to canonical EmployeeAttribute keys."""

    __tablename__ = 'employee_provider_attribute_mappings'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    application_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='CASCADE'),
        nullable=False,
    )
    employee_record_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    employee_key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    is_determinator: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text('false'),
    )
    allow_upstream: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text('false'),
    )

    application: Mapped[Application] = relationship(
        'Application',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'application_id',
            'employee_record_key',
            name='uq_emp_prov_attr_map_app_id_record_key',
        ),
    )


class EmployeeRecordMatch(Base):
    """Resolver-produced link between one EmployeeRecord and one canonical Employee."""

    __tablename__ = 'employee_record_matches'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    employee_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('employee_records.id', ondelete='CASCADE'),
        nullable=False,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('employees.id', ondelete='CASCADE'),
        nullable=False,
    )
    matched_via_determinator: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text('false'),
    )

    employee_record: Mapped[EmployeeRecord] = relationship(
        'EmployeeRecord',
        back_populates='match',
    )
    employee: Mapped[Employee] = relationship(
        'Employee',
        back_populates='record_matches',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'employee_record_id',
            name='uq_employee_record_matches_employee_record_id',
        ),
    )
