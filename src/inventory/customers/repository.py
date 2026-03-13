# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Customer repository for PostgreSQL access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.customers.models import Customer, CustomerAttribute, CustomerPlanTier, CustomerTenantRole


async def create_customer(
    session: AsyncSession,
    *,
    external_id: str,
    email_verified: bool = False,
    tenant_id: str | None = None,
    tenant_role: CustomerTenantRole | None = None,
    plan_tier: CustomerPlanTier | None = None,
    mfa_enabled: bool = True,
    is_locked: bool = False,
    description: str | None = None,
) -> Customer:
    """Create and persist a customer."""
    customer = Customer(
        external_id=external_id,
        email_verified=email_verified,
        tenant_id=tenant_id,
        tenant_role=tenant_role,
        plan_tier=plan_tier,
        mfa_enabled=mfa_enabled,
        is_locked=is_locked,
        description=description,
    )
    session.add(customer)
    await session.flush()
    await session.refresh(customer)
    return customer


async def get_customer_by_id(
    session: AsyncSession,
    customer_id: uuid.UUID,
) -> Customer | None:
    """Load customer by id."""
    result = await session.execute(select(Customer).where(Customer.id == customer_id))
    return result.scalar_one_or_none()


async def get_customer_by_external_id(
    session: AsyncSession,
    external_id: str,
) -> Customer | None:
    """Load customer by external_id."""
    result = await session.execute(select(Customer).where(Customer.external_id == external_id))
    return result.scalar_one_or_none()


async def list_customers(
    session: AsyncSession,
    *,
    plan_tier: CustomerPlanTier | None = None,
    is_locked: bool | None = None,
) -> list[Customer]:
    """List customers with optional filters."""
    query = select(Customer).order_by(Customer.id)
    if plan_tier is not None:
        query = query.where(Customer.plan_tier == plan_tier)
    if is_locked is not None:
        query = query.where(Customer.is_locked == is_locked)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_customer(
    session: AsyncSession,
    customer: Customer,
    *,
    email_verified: bool | None = None,
    mfa_enabled: bool | None = None,
    is_locked: bool | None = None,
    plan_tier: CustomerPlanTier | None = None,
) -> set[str]:
    """Apply partial update to customer. Returns set of changed field names."""
    changed: set[str] = set()
    if email_verified is not None and customer.email_verified != email_verified:
        customer.email_verified = email_verified
        changed.add('email_verified')
    if mfa_enabled is not None and customer.mfa_enabled != mfa_enabled:
        customer.mfa_enabled = mfa_enabled
        changed.add('mfa_enabled')
    if is_locked is not None and customer.is_locked != is_locked:
        customer.is_locked = is_locked
        changed.add('is_locked')
    if plan_tier is not None and customer.plan_tier != plan_tier:
        customer.plan_tier = plan_tier
        changed.add('plan_tier')
    if changed:
        await session.flush()
        await session.refresh(customer)
    return changed


async def list_customer_attributes(
    session: AsyncSession,
    customer_id: uuid.UUID,
) -> list[CustomerAttribute]:
    """List attributes for a customer."""
    result = await session.execute(
        select(CustomerAttribute).where(CustomerAttribute.customer_id == customer_id).order_by(CustomerAttribute.key)
    )
    return list(result.scalars().all())


async def create_customer_attribute(
    session: AsyncSession,
    *,
    customer_id: uuid.UUID,
    key: str,
    value: str,
) -> CustomerAttribute:
    """Create and persist a customer attribute."""
    attr = CustomerAttribute(
        customer_id=customer_id,
        key=key,
        value=value,
    )
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def get_customer_attribute_by_key(
    session: AsyncSession,
    customer_id: uuid.UUID,
    key: str,
) -> CustomerAttribute | None:
    """Load customer attribute by customer_id and key."""
    result = await session.execute(
        select(CustomerAttribute).where(
            CustomerAttribute.customer_id == customer_id,
            CustomerAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def delete_customer_attribute(
    session: AsyncSession,
    customer_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete customer attribute by customer_id and key. Returns True if deleted."""
    attr = await get_customer_attribute_by_key(session, customer_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True
