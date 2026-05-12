# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for E2 initiative.changed event emission.

Verifies:
- create_initiative → initiative.changed (change_type=created)
- update_initiative → initiative.changed (change_type=updated)
- update_initiative no-op → no initiative.changed
"""

from __future__ import annotations

import uuid

import pytest
from src.inventory.initiatives.models import InitiativeType
from src.inventory.initiatives.schemas import InitiativePatch
from src.inventory.initiatives.service import InitiativeService
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(event_service: EventService) -> InitiativeService:
    return InitiativeService(event_service=event_service)


@pytest.mark.asyncio
async def test_create_emits_initiative_changed(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_initiative emits initiative.changed with change_type=created."""
    fact_id = uuid.uuid4()
    async with session_factory() as session:
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='test',
        )
        await session.commit()

    changed = capturing_events.filter_by_type('inventory.initiative.changed')
    assert len(changed) == 1
    assert changed[0].payload['change_type'] == 'created'
    assert changed[0].payload['initiative_id'] == str(initiative.id)
    assert changed[0].payload['access_fact_id'] == str(fact_id)


@pytest.mark.asyncio
async def test_update_emits_initiative_changed(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_initiative with actual change emits initiative.changed with change_type=updated."""
    fact_id = uuid.uuid4()
    async with session_factory() as session:
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='original',
        )
        await session.commit()
        init_id = initiative.id

    capturing_events.clear()

    async with session_factory() as session:
        await service.update_initiative(session, init_id, InitiativePatch(origin='updated'))
        await session.commit()

    changed = capturing_events.filter_by_type('inventory.initiative.changed')
    assert len(changed) == 1
    assert changed[0].payload['change_type'] == 'updated'
    assert changed[0].payload['initiative_id'] == str(init_id)


@pytest.mark.asyncio
async def test_update_no_change_no_initiative_changed(
    service: InitiativeService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """update_initiative with no actual field change → no initiative.changed emitted."""
    fact_id = uuid.uuid4()
    async with session_factory() as session:
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.birthright,
            origin='same-origin',
        )
        await session.commit()
        init_id = initiative.id

    capturing_events.clear()

    async with session_factory() as session:
        # patch with same value — no field changes
        await service.update_initiative(session, init_id, InitiativePatch(origin='same-origin'))
        await session.commit()

    changed = capturing_events.filter_by_type('inventory.initiative.changed')
    assert len(changed) == 0
