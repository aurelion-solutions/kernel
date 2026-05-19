# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the unified ``inventory.nhi.updated`` fat event.

Phase 20 K-A / Slice B: ``inventory.nhi.updated`` carries
``{nhi_id, subject_ref, subject_type, changes}`` where ``subject_ref``
is ``Subject.id`` (not ``nhi_id``). The ``subject_id`` field has been removed.
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
async def test_patch_nhi_name_emits_updated_with_name_change(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH name → inventory.nhi.updated with changes.name; subject_ref = Subject.id."""
    from src.inventory.subjects.models import SubjectKind  # noqa: PLC0415
    from src.inventory.subjects.repository import get_subject_by_principal  # noqa: PLC0415

    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-ctx-e2', name='OldName', kind='bot')
        await session.flush()

        subject = await get_subject_by_principal(session, SubjectKind.nhi, nhi.id)
        assert subject is not None

        capturing_events.clear()

        updated = await service.update_nhi(session, nhi.id, NHIPatch(name='NewName'))
        await session.commit()

    assert updated.name == 'NewName'
    events = capturing_events.filter_by_type('inventory.nhi.updated')
    assert len(events) == 1
    payload = events[0].payload
    assert payload['nhi_id'] == str(nhi.id)
    assert payload['subject_ref'] == str(subject.id)
    assert payload['subject_type'] == 'nhi'
    assert 'subject_id' not in payload
    assert payload['changes'] == {
        'name': {'old': 'OldName', 'new': 'NewName'},
    }


@pytest.mark.asyncio
async def test_patch_nhi_attributes_emits_updated_with_attribute_change(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH attributes → inventory.nhi.updated with changes['attributes.<key>']."""
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-attr-e2', name='N', kind='bot')
        await session.flush()

        capturing_events.clear()

        await service.update_nhi(session, nhi.id, NHIPatch(attributes={'env': 'production'}))
        await session.commit()

    events = capturing_events.filter_by_type('inventory.nhi.updated')
    assert len(events) == 1
    assert events[0].payload['changes'] == {
        'attributes.env': {'old': None, 'new': 'production'},
    }


@pytest.mark.asyncio
async def test_deactivate_nhi_emits_expired(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """deactivate_nhi → inventory.nhi.expired with subject_ref = Subject.id."""
    from src.inventory.subjects.models import SubjectKind  # noqa: PLC0415
    from src.inventory.subjects.repository import get_subject_by_principal  # noqa: PLC0415

    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-deact-e2', name='ToDeactivate', kind='bot')
        await session.flush()

        subject = await get_subject_by_principal(session, SubjectKind.nhi, nhi.id)
        assert subject is not None

        capturing_events.clear()

        deactivated = await service.deactivate_nhi(session, nhi.id)
        await session.commit()

    assert deactivated.is_locked is True

    expired_events = capturing_events.filter_by_type('inventory.nhi.expired')
    assert len(expired_events) == 1
    payload = expired_events[0].payload
    assert payload['nhi_id'] == str(nhi.id)
    assert payload['subject_ref'] == str(subject.id)
    assert payload['subject_type'] == 'nhi'
    assert 'subject_id' not in payload


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
async def test_patch_nhi_description_emits_updated_with_description_change(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH description → updated with changes.description (was a no-event field pre-K-A)."""
    async with session_factory() as session:
        nhi = await service.create_nhi(session, external_id='nhi-desc-e2', name='N', kind='bot')
        await session.flush()

        capturing_events.clear()

        await service.update_nhi(session, nhi.id, NHIPatch(description='new desc'))
        await session.commit()

    events = capturing_events.filter_by_type('inventory.nhi.updated')
    assert len(events) == 1
    assert events[0].payload['changes'] == {
        'description': {'old': None, 'new': 'new desc'},
    }


@pytest.mark.asyncio
async def test_patch_nhi_noop_does_not_emit(
    service: NHIService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """PATCH that does not change anything emits no event."""
    async with session_factory() as session:
        nhi = await service.create_nhi(
            session,
            external_id='nhi-noop-e2',
            name='Same',
            kind='bot',
            description='same desc',
        )
        await session.flush()

        capturing_events.clear()

        await service.update_nhi(session, nhi.id, NHIPatch(name='Same', description='same desc'))
        await session.commit()

    assert capturing_events.filter_by_type('inventory.nhi.updated') == []
