# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for bulk_upsert_employees with org_unit_external_id resolution."""

from typing import Any

import pytest
from src.inventory.employees.schemas import EmployeeBulkItem
from src.inventory.employees.service import EmployeeOrgUnitNotFoundError, EmployeeService
from src.inventory.org_units.schemas import OrgUnitBulkItem
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
async def test_bulk_employees_with_org_unit_external_id(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Employee item with org_unit_external_id → org_unit_id correctly set on returned row."""
    ou_service = OrgUnitService()

    async with session_factory() as session:
        org_units = await ou_service.bulk_upsert_org_units(
            session,
            [OrgUnitBulkItem(external_id='ou-bou-test-1', name='Engineering')],
        )
        await session.commit()
    ou_id = org_units[0].id

    async with session_factory() as session:
        await create_person(session, external_id='emp-bou-1', full_name='With OU')
        await session.commit()

    items = [_item('emp-bou-1', org_unit_external_id='ou-bou-test-1')]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 1
    assert employees[0].org_unit_id == ou_id


@pytest.mark.asyncio
async def test_bulk_employees_unknown_org_unit_422(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """org_unit_external_id referencing a non-existent org_unit raises EmployeeOrgUnitNotFoundError."""
    async with session_factory() as session:
        await create_person(session, external_id='emp-bou-ghost-1', full_name='Ghost OU')
        await session.commit()

    items = [_item('emp-bou-ghost-1', org_unit_external_id='nonexistent-ou')]
    with pytest.raises(EmployeeOrgUnitNotFoundError) as exc_info:
        async with session_factory() as session:
            await service.bulk_upsert_employees(session, items)
            await session.commit()

    assert 'nonexistent-ou' in exc_info.value.missing


@pytest.mark.asyncio
async def test_bulk_employees_partial_org_unit_external_id(
    service: EmployeeService,
    session_factory: Any,
) -> None:
    """Mix of items with and without org_unit_external_id — first has org_unit_id, second has None."""
    ou_service = OrgUnitService()

    async with session_factory() as session:
        org_units = await ou_service.bulk_upsert_org_units(
            session,
            [OrgUnitBulkItem(external_id='ou-bou-partial-1', name='Partial OU')],
        )
        await session.commit()
    ou_id = org_units[0].id

    async with session_factory() as session:
        await create_person(session, external_id='emp-bou-partial-a', full_name='A')
        await create_person(session, external_id='emp-bou-partial-b', full_name='B')
        await session.commit()

    items = [
        _item('emp-bou-partial-a', org_unit_external_id='ou-bou-partial-1'),
        _item('emp-bou-partial-b'),  # no org_unit_external_id
    ]
    async with session_factory() as session:
        employees = await service.bulk_upsert_employees(session, items)
        await session.commit()

    assert len(employees) == 2

    with_ou = [e for e in employees if e.org_unit_id is not None]
    without_ou = [e for e in employees if e.org_unit_id is None]
    assert len(with_ou) == 1
    assert len(without_ou) == 1
    assert with_ou[0].org_unit_id == ou_id
