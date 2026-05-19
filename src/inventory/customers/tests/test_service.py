# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for CustomerService."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.customers.models import CustomerPlanTier
from src.inventory.customers.schemas import CustomerPatch
from src.inventory.customers.service import (
    CustomerAttributeNotFoundError,
    CustomerService,
    DuplicateCustomerAttributeError,
)
from src.platform.events.schemas import EventParticipantKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(event_service: EventService) -> CustomerService:
    return CustomerService(event_service=event_service)


# ---------------------------------------------------------------------------
# Behavioural tests (pure — no event assertions)
# ---------------------------------------------------------------------------


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
async def test_get_customer_returns_none_when_missing(
    service: CustomerService,
    session_factory,
) -> None:
    """get_customer returns None when not found."""
    async with session_factory() as session:
        result = await service.get_customer(session, uuid.uuid4())
    assert result is None


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


# ---------------------------------------------------------------------------
# Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_customer_emits_inventory_customer_created(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_customer emits inventory.customer.created with correct envelope fields."""
    async with session_factory() as session:
        customer = await service.create_customer(
            session,
            external_id='svc-log-001',
            plan_tier=CustomerPlanTier.pro,
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.customer.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.customers'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(customer.id)
    assert envelope.causation_id is None
    assert len(envelope.correlation_id) == 32
    assert envelope.correlation_id == envelope.correlation_id.lower()
    # subject_ref is Subject.id — must be set and distinct from customer.id
    assert 'subject_ref' in envelope.payload
    assert envelope.payload['subject_ref'] != str(customer.id)
    assert envelope.payload['subject_type'] == 'customer'
    assert envelope.payload['customer_id'] == str(customer.id)
    assert envelope.payload['external_id'] == 'svc-log-001'
    assert envelope.payload['tenant_id'] is None
    assert envelope.payload['plan_tier'] == 'pro'


@pytest.mark.asyncio
async def test_update_customer_emits_inventory_customer_updated(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_customer emits inventory.customer.updated with changed_fields."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-upd-001')
        await session.commit()
        customer_id = customer.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = CustomerPatch(is_locked=True)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.customer.updated')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.target_id == str(customer_id)
    assert envelope.payload['changed_fields'] == ['is_locked']
    assert envelope.payload['customer_id'] == str(customer_id)
    assert 'subject_ref' in envelope.payload
    assert envelope.payload['subject_type'] == 'customer'


@pytest.mark.asyncio
async def test_update_customer_no_op_emits_nothing(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_customer with no actual change emits zero envelopes (conditional guard).

    Replaces log-era test_update_customer_noop_no_event. Equivalent contract for no-op PATCH.
    """
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-upd-noop', is_locked=False)
        await session.commit()
        customer_id = customer.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = CustomerPatch(is_locked=False)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_add_attribute_emits_inventory_customer_attribute_added(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """add_attribute emits inventory.customer.attribute_added with owning-parent target_id."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-attr-001')
        await session.flush()
        customer_id = customer.id
        capturing_events.emitted.clear()
        await service.add_attribute(session, customer_id, 'tier', 'gold')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.customer.attribute_added')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.target_id == str(customer_id)
    assert envelope.payload == {'customer_id': str(customer_id), 'key': 'tier'}


@pytest.mark.asyncio
async def test_remove_attribute_emits_inventory_customer_attribute_removed(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """remove_attribute emits inventory.customer.attribute_removed with owning-parent target_id."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-rmlog-001')
        await session.flush()
        await service.add_attribute(session, customer.id, 'to_remove', 'x')
        await session.commit()
        customer_id = customer.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        await service.remove_attribute(session, customer_id, 'to_remove')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.customer.attribute_removed')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.target_id == str(customer_id)
    assert envelope.payload == {'customer_id': str(customer_id), 'key': 'to_remove'}


@pytest.mark.asyncio
async def test_list_customers_no_event(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """list_customers does not emit any events."""
    async with session_factory() as session:
        await service.create_customer(session, external_id='svc-list-001')
        await session.commit()

    capturing_events.emitted.clear()

    async with session_factory() as session:
        await service.list_customers(session)

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Drop-retrieved test (Q1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_customer_does_not_emit_event(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_customer emits nothing on found-path and missing-path (Q1 — customer.retrieved dropped,
    audit deferred to future audit.* slice)."""
    async with session_factory() as session:
        customer = await service.create_customer(session, external_id='svc-get-drop-001')
        await session.commit()
        customer_id = customer.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        await service.get_customer(session, customer_id)

    assert capturing_events.emitted == []

    async with session_factory() as session:
        await service.get_customer(session, uuid.uuid4())

    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Correlation-id plumbing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_customer_correlation_id_explicit(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_customer propagates an explicit correlation_id onto the envelope."""
    async with session_factory() as session:
        await service.create_customer(
            session,
            external_id='svc-corr-explicit-001',
            correlation_id='trace-xyz-789',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.customer.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-xyz-789'


@pytest.mark.asyncio
async def test_create_customer_correlation_id_autogenerated(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_customer without correlation_id generates a 32-char hex string."""
    async with session_factory() as session:
        await service.create_customer(session, external_id='svc-corr-auto-001')
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.customer.created')
    assert len(emitted) == 1
    corr = emitted[0].correlation_id
    assert len(corr) == 32
    assert corr == corr.lower()
    assert all(c in '0123456789abcdef' for c in corr)


@pytest.mark.asyncio
async def test_update_customer_correlation_id_autogenerated_independent_of_create(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_customer generates its own correlation_id, independent of the create call."""
    async with session_factory() as session:
        customer = await service.create_customer(
            session,
            external_id='svc-corr-ind-001',
            correlation_id='A' * 32,
        )
        await session.commit()
        customer_id = customer.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = CustomerPatch(is_locked=True)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.customer.updated')
    assert len(emitted) == 1
    update_corr = emitted[0].correlation_id
    assert update_corr != 'A' * 32
    assert len(update_corr) == 32
    assert all(c in '0123456789abcdef' for c in update_corr)


# ---------------------------------------------------------------------------
# Anti-dual-emit guard
# ---------------------------------------------------------------------------


def test_customer_service_has_no_log_attribute() -> None:
    """CustomerService has no _log attribute after migration (anti-dual-emit guard)."""
    svc = CustomerService()
    assert getattr(svc, '_log', None) is None


# ---------------------------------------------------------------------------
# Coupling invariant tests
# ---------------------------------------------------------------------------


def test_customer_service_bare_construction_wires_subject_service_on_shared_noop_bus() -> None:
    """CustomerService() bare wires SubjectService on the same noop_event_service instance.

    Documents the default-branch sharing invariant: when no event_service is injected,
    both CustomerService and its inner SubjectService share noop_event_service.
    """
    svc = CustomerService()
    assert svc._subject_service is not None
    assert svc._subject_service._events is svc._events


def test_customer_service_explicit_event_service_propagates_to_default_subject_service(
    event_service: EventService,
) -> None:
    """CustomerService(event_service=...) propagates it to the auto-constructed SubjectService.

    Asserts the Step-17 coupling loop closure: when caller injects event_service but not
    subject_service, the inner SubjectService shares the same bus.
    """
    svc = CustomerService(event_service=event_service)
    assert svc._subject_service._events is event_service


# ---------------------------------------------------------------------------
# Subject auto-creation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_customer_creates_subject(
    event_service: EventService,
    session_factory,
) -> None:
    """create_customer leaves exactly one Subject for the new customer."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.inventory.subjects.models import Subject, SubjectKind  # noqa: PLC0415
    from src.inventory.subjects.service import SubjectService  # noqa: PLC0415

    subject_service = SubjectService(event_service=event_service)
    svc = CustomerService(event_service=event_service, subject_service=subject_service)

    async with session_factory() as session:
        customer = await svc.create_customer(session, external_id='cust-subj-create')
        await session.commit()

    async with session_factory() as session:
        count = (
            await session.execute(
                sa.select(sa.func.count()).where(
                    Subject.kind == SubjectKind.customer,
                    Subject.principal_customer_id == customer.id,
                )
            )
        ).scalar()
    assert count == 1
