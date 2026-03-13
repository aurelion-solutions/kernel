# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for customer repository functions."""

import pytest
from src.inventory.customers.models import CustomerPlanTier
from src.inventory.customers.repository import (
    create_customer,
    create_customer_attribute,
    delete_customer_attribute,
    get_customer_attribute_by_key,
    get_customer_by_external_id,
    get_customer_by_id,
    list_customer_attributes,
    list_customers,
    update_customer,
)


@pytest.mark.asyncio
async def test_create_and_get_customer(session_factory) -> None:
    """Round-trip: create then get by id."""
    async with session_factory() as session:
        customer = await create_customer(session, external_id='ext-r-001')
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        loaded = await get_customer_by_id(session, customer_id)
    assert loaded is not None
    assert loaded.external_id == 'ext-r-001'


@pytest.mark.asyncio
async def test_get_customer_by_external_id(session_factory) -> None:
    """get_customer_by_external_id returns correct customer."""
    async with session_factory() as session:
        await create_customer(session, external_id='ext-r-002')
        await session.commit()

    async with session_factory() as session:
        loaded = await get_customer_by_external_id(session, 'ext-r-002')
    assert loaded is not None
    assert loaded.external_id == 'ext-r-002'


@pytest.mark.asyncio
async def test_list_customers(session_factory) -> None:
    """list_customers returns all rows."""
    async with session_factory() as session:
        await create_customer(session, external_id='ext-r-003')
        await create_customer(session, external_id='ext-r-004')
        await session.commit()

    async with session_factory() as session:
        customers = await list_customers(session)
    assert len(customers) >= 2


@pytest.mark.asyncio
async def test_list_customers_filter_plan_tier(session_factory) -> None:
    """list_customers filters by plan_tier."""
    async with session_factory() as session:
        await create_customer(session, external_id='ext-r-005', plan_tier=CustomerPlanTier.pro)
        await create_customer(session, external_id='ext-r-006', plan_tier=CustomerPlanTier.free)
        await session.commit()

    async with session_factory() as session:
        customers = await list_customers(session, plan_tier=CustomerPlanTier.pro)
    assert all(c.plan_tier == CustomerPlanTier.pro for c in customers)


@pytest.mark.asyncio
async def test_update_customer(session_factory) -> None:
    """update_customer changes fields and returns changed field names."""
    async with session_factory() as session:
        customer = await create_customer(session, external_id='ext-r-007')
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        loaded = await get_customer_by_id(session, customer_id)
        assert loaded is not None
        changed = await update_customer(session, loaded, is_locked=True)
        await session.commit()
    assert 'is_locked' in changed

    async with session_factory() as session:
        reloaded = await get_customer_by_id(session, customer_id)
    assert reloaded is not None
    assert reloaded.is_locked is True


@pytest.mark.asyncio
async def test_update_customer_noop(session_factory) -> None:
    """update_customer returns empty set when nothing changes."""
    async with session_factory() as session:
        customer = await create_customer(session, external_id='ext-r-008', is_locked=False)
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        loaded = await get_customer_by_id(session, customer_id)
        assert loaded is not None
        changed = await update_customer(session, loaded, is_locked=False)
        await session.commit()
    assert len(changed) == 0


@pytest.mark.asyncio
async def test_attribute_round_trip(session_factory) -> None:
    """Create, list, get, and delete customer attributes."""
    async with session_factory() as session:
        customer = await create_customer(session, external_id='ext-r-009')
        await session.flush()
        await create_customer_attribute(session, customer_id=customer.id, key='role', value='superuser')
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        attrs = await list_customer_attributes(session, customer_id)
    assert len(attrs) == 1
    assert attrs[0].key == 'role'

    async with session_factory() as session:
        found = await get_customer_attribute_by_key(session, customer_id, 'role')
    assert found is not None
    assert found.value == 'superuser'

    async with session_factory() as session:
        deleted = await delete_customer_attribute(session, customer_id, 'role')
        await session.commit()
    assert deleted is True

    async with session_factory() as session:
        attrs_after = await list_customer_attributes(session, customer_id)
    assert len(attrs_after) == 0
