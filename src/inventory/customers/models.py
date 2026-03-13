# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Customer and CustomerAttribute models."""

from __future__ import annotations

from enum import StrEnum
import uuid

import sqlalchemy as sa
from sqlalchemy import Boolean, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func
from src.core.db.base import Base


class CustomerTenantRole(StrEnum):
    """Allowed values for Customer.tenant_role. Single source of truth."""

    admin = 'admin'
    member = 'member'
    viewer = 'viewer'


class CustomerPlanTier(StrEnum):
    """Allowed values for Customer.plan_tier. Single source of truth."""

    free = 'free'
    basic = 'basic'
    pro = 'pro'
    enterprise = 'enterprise'


class Customer(Base):
    """Canonical customer (B2C/B2B) principal in the platform domain."""

    __tablename__ = 'customers'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    external_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    email_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        server_default=sa.text('false'),
    )
    tenant_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    tenant_role: Mapped[CustomerTenantRole | None] = mapped_column(
        Enum(CustomerTenantRole, name='customer_tenant_role'),
        nullable=True,
    )
    plan_tier: Mapped[CustomerPlanTier | None] = mapped_column(
        Enum(CustomerPlanTier, name='customer_plan_tier'),
        nullable=True,
    )
    mfa_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        server_default=sa.text('true'),
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

    attributes: Mapped[list[CustomerAttribute]] = relationship(
        'CustomerAttribute',
        back_populates='customer',
        cascade='all, delete-orphan',
    )


class CustomerAttribute(Base):
    """Extensible attribute attached to a Customer."""

    __tablename__ = 'customer_attributes'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('customers.id', ondelete='CASCADE'),
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

    customer: Mapped[Customer] = relationship(
        'Customer',
        back_populates='attributes',
    )

    __table_args__ = (
        sa.UniqueConstraint(
            'customer_id',
            'key',
            name='uq_customer_attributes_customer_id_key',
        ),
    )
