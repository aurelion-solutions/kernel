# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Customer and CustomerAttribute ORM models."""

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.customers.models import Customer, CustomerAttribute


@pytest.mark.asyncio
async def test_create_customer(session_factory) -> None:
    """Customer is created and persisted with defaults."""
    async with session_factory() as session:
        customer = Customer(external_id='ext-001')
        session.add(customer)
        await session.flush()
        await session.refresh(customer)
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(Customer).where(Customer.id == customer_id))
        loaded = result.scalar_one_or_none()
    assert loaded is not None
    assert loaded.external_id == 'ext-001'
    assert loaded.email_verified is False
    assert loaded.mfa_enabled is True
    assert loaded.is_locked is False
    assert loaded.tenant_id is None
    assert loaded.tenant_role is None
    assert loaded.plan_tier is None


@pytest.mark.asyncio
async def test_customer_attribute_cascade_delete(session_factory) -> None:
    """Deleting a Customer cascades to CustomerAttribute rows."""
    async with session_factory() as session:
        customer = Customer(external_id='ext-cascade')
        session.add(customer)
        await session.flush()
        attr = CustomerAttribute(
            customer_id=customer.id,
            key='foo',
            value='bar',
        )
        session.add(attr)
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(Customer).where(Customer.id == customer_id))
        customer = result.scalar_one()
        await session.delete(customer)
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(select(CustomerAttribute).where(CustomerAttribute.customer_id == customer_id))
        attrs = result.scalars().all()
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_customer_attribute_unique_constraint(session_factory) -> None:
    """Duplicate (customer_id, key) raises IntegrityError."""
    async with session_factory() as session:
        customer = Customer(external_id='ext-uniq')
        session.add(customer)
        await session.flush()
        attr1 = CustomerAttribute(customer_id=customer.id, key='k', value='v1')
        session.add(attr1)
        await session.commit()
        customer_id = customer.id

    with pytest.raises(IntegrityError):
        async with session_factory() as session:
            attr2 = CustomerAttribute(customer_id=customer_id, key='k', value='v2')
            session.add(attr2)
            await session.commit()
