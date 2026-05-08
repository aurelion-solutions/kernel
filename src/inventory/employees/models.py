# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee and EmployeeAttribute models."""

from __future__ import annotations

from typing import TYPE_CHECKING
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.db.base import Base
from src.inventory.persons.models import Person

if TYPE_CHECKING:
    from src.inventory.employee_records.models import EmployeeRecordMatch


class Employee(Base):
    """Canonical internal human identity. Belongs to Person."""

    __tablename__ = 'employees'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('persons.id', ondelete='CASCADE'),
        nullable=False,
    )
    is_locked: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sa.text('false'),
    )
    description: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    org_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('org_units.id', ondelete='SET NULL'),
        nullable=True,
    )

    person: Mapped[Person] = relationship(
        'Person',
    )
    attributes: Mapped[list[EmployeeAttribute]] = relationship(
        'EmployeeAttribute',
        back_populates='employee',
        cascade='all, delete-orphan',
    )
    record_matches: Mapped[list[EmployeeRecordMatch]] = relationship(
        'EmployeeRecordMatch',
        back_populates='employee',
        cascade='all, delete-orphan',
    )

    __table_args__ = (sa.UniqueConstraint('person_id', name='uq_employees_person_id'),)


class EmployeeAttribute(Base):
    """Extensible attribute attached to an Employee."""

    __tablename__ = 'employee_attributes'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('employees.id', ondelete='CASCADE'),
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

    employee: Mapped[Employee] = relationship(
        'Employee',
        back_populates='attributes',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'employee_id',
            'key',
            name='uq_employee_attributes_employee_id_key',
        ),
    )
