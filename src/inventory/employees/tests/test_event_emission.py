# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the unified ``inventory.employee.updated`` fat event.

Phase 20 K-A retired the two old subject-namespaced events
(``subject.context.changed``, ``subject.employment_status.changed``) in
favour of a single ``inventory.employee.updated`` carrying
``{employee_id, subject_ref, subject_type, changes: {field: {old, new}}}``.
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
async def test_patch_org_unit_emits_updated_with_org_unit_change(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH org_unit_id → inventory.employee.updated with changes.org_unit_id; subject_ref = Subject.id."""
    from src.inventory.subjects.models import SubjectKind  # noqa: PLC0415
    from src.inventory.subjects.repository import get_subject_by_principal  # noqa: PLC0415

    async with session_factory() as session:
        person = await create_person(session, external_id='p-ctx-ou-e2', full_name='OU')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()
        ou = OrgUnit(name='eng-e2', external_id='eng-e2-01')
        session.add(ou)
        await session.flush()

        # Resolve the Subject created by ensure_for_principal during create_employee.
        subject = await get_subject_by_principal(session, SubjectKind.employee, employee.id)
        assert subject is not None

        capturing_events.clear()

        updated = await service.update_employee(session, employee.id, EmployeePatch(org_unit_id=ou.id))
        await session.commit()

    assert updated.org_unit_id == ou.id
    events = capturing_events.filter_by_type('inventory.employee.updated')
    assert len(events) == 1
    payload = events[0].payload
    assert payload['employee_id'] == str(employee.id)
    assert payload['subject_ref'] == str(subject.id)
    assert payload['subject_type'] == 'employee'
    assert payload['changes'] == {
        'org_unit_id': {'old': None, 'new': str(ou.id)},
    }


@pytest.mark.asyncio
async def test_patch_role_attribute_emits_updated_with_attribute_change(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH attributes.role → inventory.employee.updated with changes['attributes.role']."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-role-e2', full_name='R')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()

        capturing_events.clear()

        await service.update_employee(session, employee.id, EmployeePatch(attributes={'role': 'engineer'}))
        await session.commit()

    events = capturing_events.filter_by_type('inventory.employee.updated')
    assert len(events) == 1
    assert events[0].payload['changes'] == {
        'attributes.role': {'old': None, 'new': 'engineer'},
    }


@pytest.mark.asyncio
async def test_patch_employment_status_emits_updated_with_employment_status_change(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH attributes.employment_status → updated with old/new under attributes.employment_status."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-empst-e2', full_name='ES')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()

        capturing_events.clear()

        await service.update_employee(session, employee.id, EmployeePatch(attributes={'employment_status': 'active'}))
        await session.commit()

    events = capturing_events.filter_by_type('inventory.employee.updated')
    assert len(events) == 1
    assert events[0].payload['changes'] == {
        'attributes.employment_status': {'old': None, 'new': 'active'},
    }


@pytest.mark.asyncio
async def test_patch_employment_status_old_value_propagated(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """When employment_status was already set, old reflects prior value."""
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

    events = capturing_events.filter_by_type('inventory.employee.updated')
    assert len(events) == 1
    assert events[0].payload['changes'] == {
        'attributes.employment_status': {'old': 'active', 'new': 'terminated'},
    }


@pytest.mark.asyncio
async def test_patch_description_emits_updated_with_description_change(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH description → inventory.employee.updated with changes.description (was a no-event field pre-K-A)."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-nodesc-e2', full_name='ND')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id)
        await session.flush()

        capturing_events.clear()

        await service.update_employee(session, employee.id, EmployeePatch(description='new desc'))
        await session.commit()

    events = capturing_events.filter_by_type('inventory.employee.updated')
    assert len(events) == 1
    assert events[0].payload['changes'] == {
        'description': {'old': None, 'new': 'new desc'},
    }


@pytest.mark.asyncio
async def test_patch_noop_does_not_emit(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH that does not change anything (all values already at target) emits no event."""
    async with session_factory() as session:
        person = await create_person(session, external_id='p-noop-e2', full_name='NO')
        await session.flush()
        employee = await service.create_employee(session, person_id=person.id, description='same')
        await session.flush()

        capturing_events.clear()

        await service.update_employee(session, employee.id, EmployeePatch(description='same'))
        await session.commit()

    assert capturing_events.filter_by_type('inventory.employee.updated') == []
