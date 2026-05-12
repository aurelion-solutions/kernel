# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for application decommission — E2 event emission.

Verifies:
- decommission_application sets is_active=False
- application.decommissioned event emitted with application_id and code
- raises ApplicationNotFoundError when application missing
"""

from __future__ import annotations

import uuid

import pytest
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.applications.schemas import ApplicationCreate
from src.platform.applications.service import create_application, decommission_application
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.mark.asyncio
async def test_decommission_sets_is_active_false(
    event_service: EventService,
    session_factory,
) -> None:
    """decommission_application sets is_active=False."""
    async with session_factory() as session:
        app = await create_application(
            session,
            ApplicationCreate(name='AppDecomm', code='app-decomm'),
        )
        await session.flush()
        app_id = app.id

        result = await decommission_application(session, app_id, event_service=event_service)
        await session.commit()

    assert result.is_active is False


@pytest.mark.asyncio
async def test_decommission_emits_application_decommissioned(
    event_service: EventService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """decommission_application emits application.decommissioned with correct payload."""
    async with session_factory() as session:
        app = await create_application(
            session,
            ApplicationCreate(name='AppDecommEvent', code='app-decomm-ev'),
        )
        await session.flush()
        app_id = app.id
        app_code = app.code

        await decommission_application(session, app_id, event_service=event_service)
        await session.commit()

    events = capturing_events.filter_by_type('inventory.application.decommissioned')
    assert len(events) == 1
    payload = events[0].payload
    assert payload['application_id'] == str(app_id)
    assert payload['code'] == app_code


@pytest.mark.asyncio
async def test_decommission_not_found_raises(
    event_service: EventService,
    session_factory,
) -> None:
    """decommission_application raises ApplicationNotFoundError when app missing."""
    with pytest.raises(ApplicationNotFoundError):
        async with session_factory() as session:
            await decommission_application(session, uuid.uuid4(), event_service=event_service)
