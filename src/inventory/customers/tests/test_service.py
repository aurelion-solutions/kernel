# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for CustomerService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.customers.models import CustomerPlanTier
from src.inventory.customers.schemas import CustomerPatch
from src.inventory.customers.service import (
    CustomerAttributeNotFoundError,
    CustomerService,
    DuplicateCustomerAttributeError,
)
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / 'logs.jsonl'


@pytest.fixture
def log_service(log_path: Path) -> LogService:
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    return LogService(factory=factory, provider_name='file')


@pytest.fixture
def service(log_service: LogService) -> CustomerService:
    return CustomerService(log_service=log_service)


@pytest.mark.asyncio
async def test_create_customer(service: CustomerService, session_factory) -> None:
    """create_customer creates and returns customer."""
    async with session_factory() as session:
        customer = await service.create_customer(
            session,
            external_id='svc-001',
        )
        await session.commit()
    assert customer.id is not None
    assert customer.external_id == 'svc-001'


@pytest.mark.asyncio
async def test_create_customer_emits_event(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """create_customer emits customer.created log event."""
    async with session_factory() as session:
        await service.create_customer(
            session,
            external_id='svc-log-001',
            plan_tier=CustomerPlanTier.pro,
        )
        await session.commit()

    assert log_path.exists()
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'customer.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'inventory.customers'
    assert 'customer_id' in created[-1]['payload']
    assert created[-1]['payload']['external_id'] == 'svc-log-001'
    assert created[-1]['payload']['plan_tier'] == 'pro'


@pytest.mark.asyncio
async def test_get_customer_emits_retrieved(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """get_customer emits customer.retrieved when found."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-get-001')
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        await service.get_customer(session, customer_id)

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'customer.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'inventory.customers'


@pytest.mark.asyncio
async def test_get_customer_returns_none_when_missing(
    service: CustomerService,
    session_factory,
) -> None:
    """get_customer returns None when not found."""
    async with session_factory() as session:
        result = await service.get_customer(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_update_customer_emits_event(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """update_customer emits customer.updated with changed_fields."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-upd-001')
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        patch = CustomerPatch(is_locked=True)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    updated = [r for r in records if r.get('event_type') == 'customer.updated']
    assert len(updated) >= 1
    assert 'is_locked' in updated[-1]['payload']['changed_fields']


@pytest.mark.asyncio
async def test_update_customer_noop_no_event(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """update_customer with no changes does not emit customer.updated."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-upd-noop', is_locked=False)
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        patch = CustomerPatch(is_locked=False)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    if log_path.exists():
        lines = log_path.read_text().strip().split('\n')
        records = [json.loads(line) for line in lines if line.strip()]
        updated = [r for r in records if r.get('event_type') == 'customer.updated']
        assert len(updated) == 0


@pytest.mark.asyncio
async def test_add_attribute_emits_event(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """add_attribute emits customer.attribute.added."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-attr-001')
        await session.flush()
        await service.add_attribute(session, customer.id, 'tier', 'gold')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    added = [r for r in records if r.get('event_type') == 'customer.attribute.added']
    assert len(added) >= 1
    assert added[-1]['payload']['key'] == 'tier'


@pytest.mark.asyncio
async def test_remove_attribute_on_missing_raises(
    service: CustomerService,
    session_factory,
) -> None:
    """remove_attribute raises CustomerAttributeNotFoundError when attribute missing."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-rm-001')
        await session.commit()
        customer_id = customer.id

    with pytest.raises(CustomerAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, customer_id, 'nonexistent')
            await session.commit()


@pytest.mark.asyncio
async def test_add_attribute_duplicate_raises(
    service: CustomerService,
    session_factory,
) -> None:
    """add_attribute raises DuplicateCustomerAttributeError on duplicate key."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-dup-001')
        await session.flush()
        await service.add_attribute(session, customer.id, 'same', 'v1')
        await session.commit()
        customer_id = customer.id

    with pytest.raises(DuplicateCustomerAttributeError):
        async with session_factory() as session:
            from src.inventory.customers.repository import get_customer_by_id

            cust = await get_customer_by_id(session, customer_id)
            assert cust is not None
            await service.add_attribute(session, cust.id, 'same', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_list_customers_no_event(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """list_customers does not emit any events."""
    async with session_factory() as session:
        await service.create_customer(session, external_id='svc-list-001')
        await session.commit()

    # Clear log by tracking line count after create
    lines_after_create = log_path.read_text().strip().split('\n') if log_path.exists() else []
    count_before = len(lines_after_create)

    async with session_factory() as session:
        await service.list_customers(session)

    lines_total = log_path.read_text().strip().split('\n') if log_path.exists() else []
    # No new lines were added by list_customers
    assert len(lines_total) == count_before


@pytest.mark.asyncio
async def test_remove_attribute_emits_event(
    service: CustomerService,
    session_factory,
    log_path: Path,
) -> None:
    """remove_attribute emits customer.attribute.removed."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-rmlog-001')
        await session.flush()
        await service.add_attribute(session, customer.id, 'to_remove', 'x')
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        await service.remove_attribute(session, customer_id, 'to_remove')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    removed = [r for r in records if r.get('event_type') == 'customer.attribute.removed']
    assert len(removed) >= 1
    assert removed[-1]['payload']['key'] == 'to_remove'
