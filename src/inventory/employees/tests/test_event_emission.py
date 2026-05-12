# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for E2 event emission in EmployeeService.

Verifies:
- PATCH org_unit_id → subject.context.changed (subject_type=employee)
- PATCH context attribute (role/project/location) → subject.context.changed
- PATCH employment_status → subject.employment_status.changed (with old/new value)
- PATCH description only → no context/status events
"""

from __future__ import annotations

import pytest
from src.inventory.employees.schemas import EmployeePatch
from src.inventory.employees.service import EmployeeService
from src.inventory.org_units.models import OrgUnit
from src.inventory.persons.repository import create_person
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(event_service: EventService) -> EmployeeService:
    return EmployeeService(event_service=event_service)


@pytest.mark.asyncio
async def test_patch_org_unit_emits_context_changed(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH org_unit_id → subject.context.changed (subject_type=employee)."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-ctx-ou-e2', full_name='OU')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        ou = OrgUnit(name='eng-e2', external_id='eng-e2-01')
        session.add(ou)
        await session.flush()

        capturing_events.clear()

        updated = await service.update_employee(session, employee.id, EmployeePatch(org_unit_id=ou.id))
        await session.commit()

    assert updated.org_unit_id == ou.id
    ctx_events = capturing_events.filter_by_type('subject.context.changed')
    assert len(ctx_events) == 1
    assert ctx_events[0].payload['subject_type'] == 'employee'
    assert ctx_events[0].payload['subject_id'] == str(employee.id)


@pytest.mark.asyncio
async def test_patch_role_attribute_emits_context_changed(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH attributes.role → subject.context.changed."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-role-e2', full_name='R')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()

        capturing_events.clear()

        await service.update_employee(session, employee.id, EmployeePatch(attributes={'role': 'engineer'}))
        await session.commit()

    ctx_events = capturing_events.filter_by_type('subject.context.changed')
    assert len(ctx_events) == 1
    assert ctx_events[0].payload['subject_type'] == 'employee'


@pytest.mark.asyncio
async def test_patch_employment_status_emits_status_changed(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH attributes.employment_status → subject.employment_status.changed with old/new."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-empst-e2', full_name='ES')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()

        capturing_events.clear()

        await service.update_employee(session, employee.id, EmployeePatch(attributes={'employment_status': 'active'}))
        await session.commit()

    status_events = capturing_events.filter_by_type('subject.employment_status.changed')
    assert len(status_events) == 1
    assert status_events[0].payload['subject_type'] == 'employee'
    assert status_events[0].payload['old_value'] is None
    assert status_events[0].payload['new_value'] == 'active'


@pytest.mark.asyncio
async def test_patch_employment_status_old_value_propagated(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """When employment_status was already set, old_value reflects prior value."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-empst2-e2', full_name='ES2')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        await service.add_attribute(session, employee.id, 'employment_status', 'active')
        await session.flush()

        capturing_events.clear()

        await service.update_employee(
            session, employee.id, EmployeePatch(attributes={'employment_status': 'terminated'})
        )
        await session.commit()

    status_events = capturing_events.filter_by_type('subject.employment_status.changed')
    assert len(status_events) == 1
    assert status_events[0].payload['old_value'] == 'active'
    assert status_events[0].payload['new_value'] == 'terminated'


@pytest.mark.asyncio
async def test_patch_description_only_no_context_events(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH description only → no context/status events emitted."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-nodesc-e2', full_name='ND')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()

        capturing_events.clear()

        await service.update_employee(session, employee.id, EmployeePatch(description='new desc'))
        await session.commit()

    assert len(capturing_events.filter_by_type('subject.context.changed')) == 0
    assert len(capturing_events.filter_by_type('subject.employment_status.changed')) == 0
