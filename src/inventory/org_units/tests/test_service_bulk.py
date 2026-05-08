# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for OrgUnitService.bulk_upsert_org_units."""

from typing import Any

import pytest
from src.inventory.org_units.schemas import OrgUnitBulkItem
from src.inventory.org_units.service import OrgUnitParentNotFoundError, OrgUnitService
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
def service(event_service: EventService) -> OrgUnitService:
    return OrgUnitService(event_service=event_service)


def _item(
    external_id: str,
    name: str,
    parent_external_id: str | None = None,
) -> OrgUnitBulkItem:
    return OrgUnitBulkItem(
        external_id=external_id,
        name=name,
        parent_external_id=parent_external_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_pass_parent_resolution_child_before_parent(
    service: OrgUnitService,
    session_factory: Any,
) -> None:
    """Child listed before parent in CSV — both rows inserted, parent_id resolved correctly."""
    items = [
        _item('ou-child-1', 'Child', parent_external_id='ou-parent-1'),
        _item('ou-parent-1', 'Parent'),
    ]
    async with session_factory() as session:
        org_units = await service.bulk_upsert_org_units(session, items)
        await session.commit()

    assert len(org_units) == 2

    # Find by external_id in returned list (ordering matches input).
    child = next(ou for ou in org_units if ou.external_id == 'ou-child-1')
    parent = next(ou for ou in org_units if ou.external_id == 'ou-parent-1')

    assert parent.parent_id is None
    assert child.parent_id == parent.id


@pytest.mark.asyncio
async def test_idempotent_reupsert(
    service: OrgUnitService,
    capturing_events: CapturingEventService,
    session_factory: Any,
) -> None:
    """Same items upserted twice — row count stays 2, name updated, IDs stable."""
    items = [
        _item('ou-idem-1', 'Department A'),
        _item('ou-idem-2', 'Department B'),
    ]
    async with session_factory() as session:
        first = await service.bulk_upsert_org_units(session, items)
        await session.commit()
    first_ids = {ou.external_id: ou.id for ou in first}

    # Second call with updated names.
    items2 = [
        _item('ou-idem-1', 'Department A Updated'),
        _item('ou-idem-2', 'Department B Updated'),
    ]
    async with session_factory() as session:
        second = await service.bulk_upsert_org_units(session, items2)
        _ = second  # used below
        await session.commit()

    assert len(second) == 2
    for ou in second:
        assert ou.id == first_ids[ou.external_id]
        assert 'Updated' in ou.name


@pytest.mark.asyncio
async def test_unknown_parent_raises(
    service: OrgUnitService,
    session_factory: Any,
) -> None:
    """parent_external_id references an org_unit not in the batch and not in DB → raises."""
    items = [
        _item('ou-orphan-child', 'Child', parent_external_id='ou-nonexistent-parent'),
    ]
    with pytest.raises(OrgUnitParentNotFoundError) as exc_info:
        async with session_factory() as session:
            await service.bulk_upsert_org_units(session, items)
            await session.commit()

    assert 'ou-nonexistent-parent' in exc_info.value.missing


@pytest.mark.asyncio
async def test_one_event_emitted(
    service: OrgUnitService,
    capturing_events: CapturingEventService,
    session_factory: Any,
) -> None:
    """Exactly ONE inventory.org_unit.bulk_upserted event with correct payload."""
    items = [
        _item('ou-evt-1', 'Engineering'),
        _item('ou-evt-2', 'HR'),
    ]
    async with session_factory() as session:
        await service.bulk_upsert_org_units(session, items)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.org_unit.bulk_upserted')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.payload['count'] == 2
    assert set(envelope.payload['external_ids']) == {'ou-evt-1', 'ou-evt-2'}


@pytest.mark.asyncio
async def test_parent_in_db_resolved_across_batches(
    service: OrgUnitService,
    session_factory: Any,
) -> None:
    """Parent already in DB from a previous batch — child batch resolves it via SELECT IN."""
    # First batch — insert parent only.
    async with session_factory() as session:
        await service.bulk_upsert_org_units(session, [_item('ou-db-parent', 'DB Parent')])
        await session.commit()

    # Second batch — child references pre-existing parent.
    async with session_factory() as session:
        second = await service.bulk_upsert_org_units(
            session,
            [_item('ou-db-child', 'DB Child', parent_external_id='ou-db-parent')],
        )
        await session.commit()

    assert len(second) == 1
    child = second[0]
    assert child.parent_id is not None
