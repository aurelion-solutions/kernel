# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for CustomerService -> SubjectService.recompute_status_for_principal hook."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.customers.schemas import CustomerPatch
from src.inventory.customers.service import CustomerService
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.service import SubjectService
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def subject_service(event_service: EventService) -> SubjectService:
    return SubjectService(event_service=event_service)


@pytest.fixture
def service(event_service: EventService, subject_service: SubjectService) -> CustomerService:
    """CustomerService sharing one EventService with SubjectService."""
    return CustomerService(event_service=event_service, subject_service=subject_service)


@pytest.mark.asyncio
async def test_update_customer_locks_flips_subject_status_to_suspended(
    service: CustomerService,
    subject_service: SubjectService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH is_locked=True on Customer with a bound verified Subject -> Subject.status=suspended.

    Also asserts event order: customer.updated emitted before subject.status_changed.
    """
    async with session_factory() as session:
        from src.inventory.customers.repository import create_customer
        from src.inventory.subjects.repository import create_subject

        customer = await create_customer(
            session,
            external_id=str(uuid.uuid4()),
            email_verified=True,
            is_locked=False,
        )
        await session.flush()

        subject = await create_subject(
            session,
            external_id=str(uuid.uuid4()),
            kind=SubjectKind.customer,
            principal_customer_id=customer.id,
            status='verified',
        )
        await session.commit()
        customer_id = customer.id
        subject_id = subject.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = CustomerPatch(is_locked=True)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    # Assert Subject row in DB updated
    async with session_factory() as session:
        from src.inventory.subjects.repository import get_subject_by_id

        updated_subject = await get_subject_by_id(session, subject_id)
        assert updated_subject is not None
        assert updated_subject.status == 'suspended'

    # Assert event emission via capturing_events
    customer_updated = capturing_events.filter_by_type('inventory.customer.updated')
    status_changed = capturing_events.filter_by_type('inventory.subject.status_changed')

    assert len(customer_updated) >= 1
    assert 'is_locked' in customer_updated[-1].payload['changed_fields']

    assert len(status_changed) == 1
    assert status_changed[0].payload['previous_status'] == 'verified'
    assert status_changed[0].payload['new_status'] == 'suspended'
    assert 'subject_id' in status_changed[0].payload

    # Assert order: customer.updated emitted before subject.status_changed
    all_emitted = capturing_events.emitted
    cu_idx = next(i for i, e in enumerate(all_emitted) if e.event_type == 'inventory.customer.updated')
    sc_idx = next(i for i, e in enumerate(all_emitted) if e.event_type == 'inventory.subject.status_changed')
    assert cu_idx < sc_idx


@pytest.mark.asyncio
async def test_update_customer_no_status_relevant_change_emits_no_status_event(
    service: CustomerService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH plan_tier does not trigger subject.status_changed (plan_tier not in guard set)."""
    from src.inventory.customers.models import CustomerPlanTier

    async with session_factory() as session:
        from src.inventory.customers.repository import create_customer
        from src.inventory.subjects.repository import create_subject

        customer = await create_customer(
            session,
            external_id=str(uuid.uuid4()),
            email_verified=True,
            is_locked=False,
        )
        await session.flush()

        await create_subject(
            session,
            external_id=str(uuid.uuid4()),
            kind=SubjectKind.customer,
            principal_customer_id=customer.id,
            status='verified',
        )
        await session.commit()
        customer_id = customer.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        patch = CustomerPatch(plan_tier=CustomerPlanTier.pro)
        await service.update_customer(session, customer_id, patch)
        await session.commit()

    # customer.updated should be emitted (plan_tier changed)
    customer_updated = capturing_events.filter_by_type('inventory.customer.updated')
    assert len(customer_updated) >= 1

    # subject.status_changed must NOT be emitted
    status_changed = capturing_events.filter_by_type('inventory.subject.status_changed')
    assert len(status_changed) == 0


def test_customer_and_subject_services_share_event_sink(
    service: CustomerService,
    subject_service: SubjectService,
) -> None:
    """CustomerService and SubjectService share the same EventService instance.

    Architectural canary: protects against a future refactor that reintroduces separate sinks.
    """
    assert service._events is subject_service._events
