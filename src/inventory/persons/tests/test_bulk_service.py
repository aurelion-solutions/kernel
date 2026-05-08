# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PersonService.bulk_upsert_persons."""

import pytest
from src.inventory.persons.repository import create_person
from src.inventory.persons.schemas import PersonBulkItem
from src.inventory.persons.service import PersonService
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
def service(event_service: EventService) -> PersonService:
    return PersonService(event_service=event_service)


def _item(external_id: str, full_name: str) -> PersonBulkItem:
    return PersonBulkItem(external_id=external_id, full_name=full_name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_upsert_inserts_new_persons(
    service: PersonService,
    session_factory,
) -> None:
    """3 new items — all inserted, ids returned in input order."""
    items = [
        _item('bulk-a', 'Alice'),
        _item('bulk-b', 'Bob'),
        _item('bulk-c', 'Carol'),
    ]
    async with session_factory() as session:
        persons = await service.bulk_upsert_persons(session, items)
        await session.commit()

    assert len(persons) == 3
    ext_ids = [p.external_id for p in persons]
    assert ext_ids == ['bulk-a', 'bulk-b', 'bulk-c']
    for p in persons:
        assert p.id is not None


@pytest.mark.asyncio
async def test_bulk_upsert_updates_existing_by_external_id(
    service: PersonService,
    session_factory,
) -> None:
    """Pre-seed 1 person, upsert 2 items (1 matching, 1 new) — update + insert; existing id preserved."""
    async with session_factory() as session:
        existing = await create_person(session, external_id='bulk-upd-existing', full_name='Old Name')
        await session.commit()
        existing_id = existing.id

    items = [
        _item('bulk-upd-existing', 'New Name'),
        _item('bulk-upd-new', 'Brand New'),
    ]
    async with session_factory() as session:
        persons = await service.bulk_upsert_persons(session, items)
        await session.commit()

    assert len(persons) == 2
    updated = persons[0]
    assert updated.id == existing_id
    assert updated.external_id == 'bulk-upd-existing'
    assert updated.full_name == 'New Name'

    inserted = persons[1]
    assert inserted.external_id == 'bulk-upd-new'


@pytest.mark.asyncio
async def test_bulk_upsert_replace_all_semantics(
    service: PersonService,
    session_factory,
) -> None:
    """Pre-seed person with full_name='old', upsert same external_id with full_name='new' — description == 'new'."""
    async with session_factory() as session:
        await create_person(session, external_id='bulk-replace', full_name='old')
        await session.commit()

    items = [_item('bulk-replace', 'new')]
    async with session_factory() as session:
        persons = await service.bulk_upsert_persons(session, items)
        await session.commit()

    assert len(persons) == 1
    assert persons[0].full_name == 'new'


@pytest.mark.asyncio
async def test_bulk_upsert_emits_single_event(
    service: PersonService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Exactly ONE inventory.person.bulk_upserted event emitted with correct count."""
    items = [
        _item('bulk-evt-1', 'One'),
        _item('bulk-evt-2', 'Two'),
    ]
    async with session_factory() as session:
        await service.bulk_upsert_persons(session, items)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.person.bulk_upserted')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.payload['count'] == 2
    assert set(envelope.payload['external_ids']) == {'bulk-evt-1', 'bulk-evt-2'}
