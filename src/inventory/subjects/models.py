# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject model — canonical actor abstraction over Employee / NHI / Customer."""

from __future__ import annotations

from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import CheckConstraint, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func, text
from src.core.db.base import Base


class SubjectKind(StrEnum):
    """Closed set of principal kinds. Single source of truth."""

    employee = 'employee'
    nhi = 'nhi'
    customer = 'customer'


class SubjectNHIKind(StrEnum):
    """NHI sub-kind vocabulary. Non-null IFF kind == nhi."""

    service_account = 'service_account'
    api_key = 'api_key'
    bot = 'bot'
    certificate = 'certificate'


class SubjectEmployeeStatus(StrEnum):
    """Allowed status values when kind == employee."""

    hired = 'hired'
    active = 'active'
    on_leave = 'on_leave'
    terminated = 'terminated'


class SubjectNHIStatus(StrEnum):
    """Allowed status values when kind == nhi."""

    active = 'active'
    expired = 'expired'
    locked = 'locked'


class SubjectCustomerStatus(StrEnum):
    """Allowed status values when kind == customer."""

    registered = 'registered'
    verified = 'verified'
    active = 'active'
    suspended = 'suspended'
    banned = 'banned'
    deletion_requested = 'deletion_requested'


SubjectStatus = SubjectEmployeeStatus | SubjectNHIStatus | SubjectCustomerStatus


_PRINCIPAL_EXACTLY_ONE = (
    "(kind = 'employee' AND principal_employee_id IS NOT NULL"
    ' AND principal_nhi_id IS NULL AND principal_customer_id IS NULL)'
    " OR (kind = 'nhi' AND principal_nhi_id IS NOT NULL"
    ' AND principal_employee_id IS NULL AND principal_customer_id IS NULL)'
    " OR (kind = 'customer' AND principal_customer_id IS NOT NULL"
    ' AND principal_employee_id IS NULL AND principal_nhi_id IS NULL)'
)

_NHI_KIND_CONSISTENCY = "(kind = 'nhi' AND nhi_kind IS NOT NULL) OR (kind != 'nhi' AND nhi_kind IS NULL)"

_STATUS_VOCABULARY = (
    "(kind = 'employee' AND status IN ('hired', 'active', 'on_leave', 'terminated'))"
    " OR (kind = 'nhi' AND status IN ('active', 'expired', 'locked'))"
    " OR (kind = 'customer' AND status IN"
    " ('registered', 'verified', 'active', 'suspended', 'banned', 'deletion_requested'))"
)


class Subject(Base):
    """Canonical actor abstraction that wraps one principal (Employee, NHI, or Customer)."""

    __tablename__ = 'subjects'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    kind: Mapped[SubjectKind] = mapped_column(
        Enum(SubjectKind, name='subject_kind'),
        nullable=False,
    )
    nhi_kind: Mapped[SubjectNHIKind | None] = mapped_column(
        Enum(SubjectNHIKind, name='subject_nhi_kind'),
        nullable=True,
    )
    principal_employee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('employees.id', ondelete='RESTRICT'),
        nullable=True,
    )
    principal_nhi_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('nhis.id', ondelete='RESTRICT'),
        nullable=True,
    )
    principal_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('customers.id', ondelete='RESTRICT'),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
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
        CheckConstraint(
            _PRINCIPAL_EXACTLY_ONE,
            name='ck_subjects_principal_exactly_one',
        ),
        CheckConstraint(
            _NHI_KIND_CONSISTENCY,
            name='ck_subjects_nhi_kind_consistency',
        ),
        CheckConstraint(
            _STATUS_VOCABULARY,
            name='ck_subjects_status_vocabulary',
        ),
        sa.UniqueConstraint(
            'kind',
            'external_id',
            name='uq_subjects_kind_external_id',
        ),
        Index(
            'uq_subjects_principal_employee_id',
            'principal_employee_id',
            unique=True,
            postgresql_where=text('principal_employee_id IS NOT NULL'),
        ),
        Index(
            'uq_subjects_principal_nhi_id',
            'principal_nhi_id',
            unique=True,
            postgresql_where=text('principal_nhi_id IS NOT NULL'),
        ),
        Index(
            'uq_subjects_principal_customer_id',
            'principal_customer_id',
            unique=True,
            postgresql_where=text('principal_customer_id IS NOT NULL'),
        ),
        Index('ix_subjects_kind_status', 'kind', 'status'),
    )

    attributes: Mapped[list[SubjectAttribute]] = relationship(
        'SubjectAttribute',
        back_populates='subject',
        cascade='all, delete-orphan',
    )


class SubjectAttribute(Base):
    """Extensible key/value attribute attached to a Subject."""

    __tablename__ = 'subject_attributes'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('subjects.id', ondelete='CASCADE'),
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

    subject: Mapped[Subject] = relationship(
        'Subject',
        back_populates='attributes',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'subject_id',
            'key',
            name='uq_subject_attributes_subject_id_key',
        ),
    )
