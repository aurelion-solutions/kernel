# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for E2 event emission in NHIService.

Verifies:
- PATCH context fields → subject.context.changed (subject_type=nhi)
- PATCH attributes → subject.context.changed (subject_type=nhi)
- deactivate_nhi → nhi.expired
- PATCH non-context fields (description, is_locked) → no subject.context.changed
"""

from __future__ import annotations

import pytest
from src.inventory.nhi.schemas import NHIPatch
from src.inventory.nhi.service import NHINotFoundError, NHIService
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(event_service: EventService) -> NHIService:
    return NHIService(event_service=event_service)


@pytest.mark.asyncio
async def test_patch_nhi_name_emits_context_changed(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH name (context field) → subject.context.changed (subject_type=nhi)."""
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-ctx-e2', name='OldName', kind='bot')
        await session.flush()

        capturing_events.clear()

        updated = await service.update_nhi(session, nhi.id, NHIPatch(name='NewName'))
        await session.commit()

    assert updated.name == 'NewName'
    ctx_events = capturing_events.filter_by_type('subject.context.changed')
    assert len(ctx_events) == 1
    assert ctx_events[0].payload['subject_type'] == 'nhi'
    assert ctx_events[0].payload['subject_id'] == str(nhi.id)


@pytest.mark.asyncio
async def test_patch_nhi_attributes_emits_context_changed(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH attributes → subject.context.changed (subject_type=nhi)."""
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-attr-e2', name='N', kind='bot')
        await session.flush()

        capturing_events.clear()

        await service.update_nhi(session, nhi.id, NHIPatch(attributes={'env': 'production'}))
        await session.commit()

    ctx_events = capturing_events.filter_by_type('subject.context.changed')
    assert len(ctx_events) == 1
    assert ctx_events[0].payload['subject_type'] == 'nhi'


@pytest.mark.asyncio
async def test_deactivate_nhi_emits_expired(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """deactivate_nhi → nhi.expired event emitted."""
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-deact-e2', name='ToDeactivate', kind='bot')
        await session.flush()

        capturing_events.clear()

        deactivated = await service.deactivate_nhi(session, nhi.id)
        await session.commit()

    assert deactivated.is_locked is True

    expired_events = capturing_events.filter_by_type('inventory.nhi.expired')
    assert len(expired_events) == 1
    assert expired_events[0].payload['nhi_id'] == str(nhi.id)
    assert expired_events[0].payload['subject_type'] == 'nhi'


@pytest.mark.asyncio
async def test_deactivate_nhi_not_found_raises(
    service: NHIService,
    session_factory,
) -> None:
    """deactivate_nhi raises NHINotFoundError when NHI missing."""
    import uuid

    with pytest.raises(NHINotFoundError):
        async with session_factory() as session:
            await service.deactivate_nhi(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_patch_nhi_description_no_context_event(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH description (non-context field) → no subject.context.changed."""
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-desc-e2', name='N', kind='bot')
        await session.flush()

        capturing_events.clear()

        await service.update_nhi(session, nhi.id, NHIPatch(description='new desc'))
        await session.commit()

    assert len(capturing_events.filter_by_type('subject.context.changed')) == 0
