# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeService.bulk_upsert_employees."""

from typing import Any

import pytest
from src.inventory.employees.repository import create_employee
from src.inventory.employees.schemas import EmployeeBulkItem
from src.inventory.employees.service import EmployeeOrgUnitNotFoundError, EmployeeService, UnknownPersonExternalIdsError
from src.inventory.org_units.service import OrgUnitService
from src.inventory.persons.repository import create_person
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(event_service: EventService) -> EmployeeService:
    return EmployeeService(event_service=event_service)


def _item(
    person_external_id: str,
    is_locked: bool = False,
    description: str | None = None,
    org_unit_external_id: str | None = None,
) -> EmployeeBulkItem:
    return EmployeeBulkItem(
        person_external_id=person_external_id,
        is_locked=is_locked,
        description=description,
        org_unit_external_id=org_unit_external_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_inserts_new_employees(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """3 new persons + 3 items — all inserted, ids returned in input order."""
    async with session_factory() as session:
        p1 = await create_person(session, external_id='bulk-emp-a', full_name='A')
        p2 = await create_person(session, external_id='bulk-emp-b', full_name='B')
        p3 = await create_person(session, external_id='bulk-emp-c', full_name='C')
        await session.commit()
        assert p1.id and p2.id and p3.id

    items = [
        _item('bulk-emp-a'),
        _item('bulk-emp-b'),
        _item('bulk-emp-c'),
    ]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 3
    for emp in employees:
        assert emp.id is not None


@pytest.mark.asyncio
async def test_bulk_upsert_updates_existing_by_person_id(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Pre-seed person + employee, upsert with same external_id — update, same Employee.id."""
    async with session_factory() as session:
        person = await create_person(session, external_id='bulk-emp-upd', full_name='Upd')
        await session.flush()
        emp = await create_employee(
            session,
            person_id=person.id,
            is_locked=False,
            description='old',
        )
        await session.commit()
        original_emp_id = emp.id

    items = [_item('bulk-emp-upd', is_locked=True, description='new')]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 1
    updated = employees[0]
    assert updated.id == original_emp_id
    assert updated.is_locked is True
    assert updated.description == 'new'


@pytest.mark.asyncio
async def test_bulk_upsert_replace_all_clears_description_when_none(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Pre-seed employee with description set, upsert with description=None — description IS NULL."""
    async with session_factory() as session:
        person = await create_person(session, external_id='bulk-emp-null-desc', full_name='Null')
        await session.flush()
        await create_employee(
            session,
            person_id=person.id,
            is_locked=False,
            description='some description',
        )
        await session.commit()

    items = [_item('bulk-emp-null-desc', description=None)]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 1
    assert employees[0].description is None


@pytest.mark.asyncio
async def test_bulk_upsert_raises_on_unknown_person_external_id(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Submit item with non-existent person_external_id — raises UnknownPersonExternalIdsError."""
    items = [_item('bulk-emp-ghost-person')]
    with pytest.raises(UnknownPersonExternalIdsError) as exc_info:
        async with session_factory() as session:
            await service.bulk_upsert_employees(session, items)
            await session.commit()

    assert 'bulk-emp-ghost-person' in exc_info.value.missing


@pytest.mark.asyncio
async def test_bulk_upsert_mixed_insert_and_update(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """2 existing employees + 2 new persons — 4 rows total, existing ids preserved."""
    async with session_factory() as session:
        p1 = await create_person(session, external_id='bulk-emp-mix-1', full_name='Mix1')
        p2 = await create_person(session, external_id='bulk-emp-mix-2', full_name='Mix2')
        p3 = await create_person(session, external_id='bulk-emp-mix-3', full_name='Mix3')
        p4 = await create_person(session, external_id='bulk-emp-mix-4', full_name='Mix4')
        await session.flush()
        emp1 = await create_employee(session, person_id=p1.id, is_locked=False, description='existing-1')
        emp2 = await create_employee(session, person_id=p2.id, is_locked=False, description='existing-2')
        await session.commit()
        emp1_id = emp1.id
        emp2_id = emp2.id
        p3_id = p3.id
        p4_id = p4.id

    items = [
        _item('bulk-emp-mix-1', description='updated-1'),
        _item('bulk-emp-mix-2', description='updated-2'),
        _item('bulk-emp-mix-3', description='new-3'),
        _item('bulk-emp-mix-4', description='new-4'),
    ]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 4

    # Existing ids preserved
    assert employees[0].id == emp1_id
    assert employees[1].id == emp2_id

    # New employees created (different ids from existing ones)
    assert employees[2].id not in (emp1_id, emp2_id)
    assert employees[3].id not in (emp1_id, emp2_id)

    # Person references correct
    assert employees[2].person_id == p3_id
    assert employees[3].person_id == p4_id

    # Descriptions updated
    assert employees[0].description == 'updated-1'
    assert employees[1].description == 'updated-2'


@pytest.mark.asyncio
async def test_bulk_upsert_emits_single_event(
    service: EmployeeService,
    capturing_events: CapturingEventService,
    session_factory: Any,
) -> None:
    """Exactly ONE inventory.employee.bulk_upserted event with correct count and person_ids."""
    async with session_factory() as session:
        await create_person(session, external_id='bulk-emp-evt-1', full_name='Evt1')
        await create_person(session, external_id='bulk-emp-evt-2', full_name='Evt2')
        await session.commit()

    items = [
        _item('bulk-emp-evt-1'),
        _item('bulk-emp-evt-2'),
    ]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.employee.bulk_upserted')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.payload['count'] == 2
    returned_person_ids = set(envelope.payload['person_ids'])
    expected_person_ids = {str(e.person_id) for e in employees}
    assert returned_person_ids == expected_person_ids


@pytest.mark.asyncio
async def test_bulk_employees_with_org_unit_external_id(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Employee item with org_unit_external_id → org_unit_id set on returned row."""
    ou_service = OrgUnitService()
    async with session_factory() as session:
        from src.inventory.org_units.schemas import OrgUnitBulkItem

        ou_items = [OrgUnitBulkItem(external_id='ou-emp-test-1', name='Engineering')]
        org_units = await ou_service.bulk_upsert_org_units(session, ou_items)
        await session.commit()
    ou_id = org_units[0].id

    async with session_factory() as session:
        await create_person(session, external_id='emp-with-ou-1', full_name='With OU')
        await session.commit()

    items = [_item('emp-with-ou-1', org_unit_external_id='ou-emp-test-1')]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 1
    assert employees[0].org_unit_id == ou_id


@pytest.mark.asyncio
async def test_bulk_employees_unknown_org_unit_raises(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """org_unit_external_id referencing a non-existent org_unit → EmployeeOrgUnitNotFoundError."""
    async with session_factory() as session:
        await create_person(session, external_id='emp-ghost-ou-1', full_name='Ghost OU')
        await session.commit()

    items = [_item('emp-ghost-ou-1', org_unit_external_id='ou-nonexistent-999')]
    with pytest.raises(EmployeeOrgUnitNotFoundError) as exc_info:
        async with session_factory() as session:
            await service.bulk_upsert_employees(session, items)
            await session.commit()

    assert 'ou-nonexistent-999' in exc_info.value.missing


@pytest.mark.asyncio
async def test_bulk_employees_partial_org_unit_external_id(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Mix of items with and without org_unit_external_id — works correctly."""
    ou_service = OrgUnitService()
    async with session_factory() as session:
        from src.inventory.org_units.schemas import OrgUnitBulkItem

        ou_items = [OrgUnitBulkItem(external_id='ou-partial-1', name='Partial OU')]
        org_units = await ou_service.bulk_upsert_org_units(session, ou_items)
        await session.commit()
    ou_id = org_units[0].id

    async with session_factory() as session:
        await create_person(session, external_id='emp-partial-a', full_name='A')
        await create_person(session, external_id='emp-partial-b', full_name='B')
        await session.commit()

    items = [
        _item('emp-partial-a', org_unit_external_id='ou-partial-1'),
        _item('emp-partial-b'),  # no org_unit_external_id
    ]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 2
    # Find by checking org_unit_id
    with_ou = [e for e in employees if e.org_unit_id is not None]
    without_ou = [e for e in employees if e.org_unit_id is None]
    assert len(with_ou) == 1
    assert len(without_ou) == 1
    assert with_ou[0].org_unit_id == ou_id


@pytest.mark.asyncio
async def test_bulk_employees_no_org_unit_regression(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Regression: org_unit_external_id absent → behavior identical to before the field existed."""
    async with session_factory() as session:
        await create_person(session, external_id='emp-regression-ou', full_name='Regression')
        await session.commit()

    items = [_item('emp-regression-ou', is_locked=True, description='reg-desc')]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 1
    emp = employees[0]
    assert emp.is_locked is True
    assert emp.description == 'reg-desc'
    assert emp.org_unit_id is None
