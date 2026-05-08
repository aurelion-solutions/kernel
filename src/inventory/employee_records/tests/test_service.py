# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeRecordService."""

import uuid

import pytest
from src.inventory.employee_records.repository import (
    get_employee_record_by_external_id,
)
from src.inventory.employee_records.service import (
    DuplicateEmployeeRecordAttributeError,
    EmployeeRecordAttributeNotFoundError,
    EmployeeRecordNotFoundError,
    EmployeeRecordService,
    InvalidApplicationIdError,
)
from src.platform.applications.models import Application
from src.platform.events.schemas import EventParticipantKind
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
def service(event_service: EventService) -> EmployeeRecordService:
    return EmployeeRecordService(event_service=event_service)


@pytest.fixture
async def application_id(session_factory):
    """Create an application for employee record tests."""
    async with session_factory() as session:
        app = Application(name='hr-app', code='hr-app')
        session.add(app)
        await session.commit()
        return app.id


# ---------------------------------------------------------------------------
# Behavioural tests (state transitions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee_record(
    service: EmployeeRecordService, session_factory, application_id: uuid.UUID
) -> None:
    """create_employee_record creates and returns record."""
    async with session_factory() as session:
        record = await service.create_employee_record(
            session,
            external_id='rec-svc',
            application_id=application_id,
        )
        await session.commit()
    assert record.id is not None
    assert record.external_id == 'rec-svc'
    assert record.application_id == application_id


@pytest.mark.asyncio
async def test_create_employee_record_invalid_application_id_raises(
    service: EmployeeRecordService,
    session_factory,
) -> None:
    """create_employee_record raises InvalidApplicationIdError when application_id not found."""
    with pytest.raises(InvalidApplicationIdError):
        async with session_factory() as session:
            await service.create_employee_record(
                session,
                external_id='rec-bad',
                application_id=uuid.uuid4(),
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_employee_record(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """get_employee_record returns record when found."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-get', application_id=application_id)
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        loaded = await service.get_employee_record(session, record_id)
    assert loaded is not None
    assert loaded.id == record_id


@pytest.mark.asyncio
async def test_get_employee_record_returns_none_when_missing(
    service: EmployeeRecordService,
    session_factory,
) -> None:
    """get_employee_record returns None when not found."""
    async with session_factory() as session:
        result = await service.get_employee_record(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_list_employee_records(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """list_employee_records returns all records."""
    async with session_factory() as session:
        await service.create_employee_record(session, external_id='rec-1', application_id=application_id)
        await service.create_employee_record(session, external_id='rec-2', application_id=application_id)
        await session.commit()

    async with session_factory() as session:
        records = await service.list_employee_records(session)
    assert len(records) >= 2


@pytest.mark.asyncio
async def test_list_attributes(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """list_attributes returns attributes for employee record."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-la', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'attr1', 'val1')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        attrs = await service.list_attributes(session, record_id)
    assert len(attrs) == 1
    assert attrs[0].key == 'attr1'
    assert attrs[0].value == 'val1'


@pytest.mark.asyncio
async def test_list_attributes_raises_when_record_missing(
    service: EmployeeRecordService,
    session_factory,
) -> None:
    """list_attributes raises EmployeeRecordNotFoundError when record missing."""
    with pytest.raises(EmployeeRecordNotFoundError):
        async with session_factory() as session:
            await service.list_attributes(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_add_attribute(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """add_attribute adds and returns attribute."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-add', application_id=application_id)
        await session.flush()
        attr = await service.add_attribute(session, record.id, 'newkey', 'newval')
        await session.commit()
    assert attr.id is not None
    assert attr.key == 'newkey'
    assert attr.value == 'newval'


@pytest.mark.asyncio
async def test_add_attribute_duplicate_key_raises(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """add_attribute raises DuplicateEmployeeRecordAttributeError on duplicate key."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-dup', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'same', 'v1')
        await session.commit()

    with pytest.raises(DuplicateEmployeeRecordAttributeError):
        async with session_factory() as session:
            rec = await get_employee_record_by_external_id(session, 'rec-dup', application_id)
            assert rec is not None
            await service.add_attribute(session, rec.id, 'same', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_remove_attribute(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """remove_attribute removes attribute."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-rm', application_id=application_id)
        await session.flush()
        await service.add_attribute(session, record.id, 'todel', 'x')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        await service.remove_attribute(session, record_id, 'todel')
        await session.commit()

    async with session_factory() as session:
        attrs = await service.list_attributes(session, record_id)
    assert len(attrs) == 0


@pytest.mark.asyncio
async def test_remove_attribute_raises_when_missing(
    service: EmployeeRecordService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """remove_attribute raises EmployeeRecordAttributeNotFoundError when attribute missing."""
    async with session_factory() as session:
        record = await service.create_employee_record(session, external_id='rec-norm', application_id=application_id)
        await session.commit()
        record_id = record.id

    with pytest.raises(EmployeeRecordAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, record_id, 'nonexistent')
            await session.commit()


# ---------------------------------------------------------------------------
# Event-emission tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee_record_emits_inventory_employee_record_created(
    service: EmployeeRecordService,
    capturing_events: CapturingEventService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """create_employee_record emits inventory.employee_record.created with correct envelope fields."""
    async with session_factory() as session:
        record = await service.create_employee_record(
            session,
            external_id='emit-c',
            application_id=application_id,
        )
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.employee_record.created')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.employee_records'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(record.id)
    assert envelope.causation_id is None
    assert isinstance(envelope.correlation_id, str)
    assert len(envelope.correlation_id) > 0
    assert envelope.payload == {'employee_record_id': str(record.id), 'external_id': record.external_id}


@pytest.mark.asyncio
async def test_add_attribute_emits_inventory_employee_record_attribute_added(
    service: EmployeeRecordService,
    capturing_events: CapturingEventService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """add_attribute emits inventory.employee_record.attribute_added with owning-parent target_id."""
    async with session_factory() as session:
        record = await service.create_employee_record(
            session,
            external_id='emit-a',
            application_id=application_id,
        )
        await session.flush()
        capturing_events.emitted.clear()
        await service.add_attribute(session, record.id, 'k1', 'v1')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.employee_record.attribute_added')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.target_id == str(record.id)
    assert envelope.payload == {'employee_record_id': str(record.id), 'key': 'k1'}


@pytest.mark.asyncio
async def test_remove_attribute_emits_inventory_employee_record_attribute_removed(
    service: EmployeeRecordService,
    capturing_events: CapturingEventService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """remove_attribute emits inventory.employee_record.attribute_removed with owning-parent target_id."""
    async with session_factory() as session:
        record = await service.create_employee_record(
            session,
            external_id='emit-r',
            application_id=application_id,
        )
        await session.flush()
        await service.add_attribute(session, record.id, 'rk', 'rv')
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        capturing_events.emitted.clear()
        await service.remove_attribute(session, record_id, 'rk')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.employee_record.attribute_removed')
    assert len(envelopes) == 1
    envelope = envelopes[0]
    assert envelope.target_id == str(record_id)
    assert envelope.payload == {'employee_record_id': str(record_id), 'key': 'rk'}


# ---------------------------------------------------------------------------
# Drop-retrieved test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_employee_record_does_not_emit_event(
    service: EmployeeRecordService,
    capturing_events: CapturingEventService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """get_employee_record emits no event (Q1 — employee_record.retrieved dropped)."""
    async with session_factory() as session:
        record = await service.create_employee_record(
            session,
            external_id='no-emit',
            application_id=application_id,
        )
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        capturing_events.emitted.clear()
        result = await service.get_employee_record(session, record_id)
        assert result is not None
        assert capturing_events.emitted == []

    async with session_factory() as session:
        result = await service.get_employee_record(session, uuid.uuid4())
        assert result is None
        assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Correlation-id tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_employee_record_correlation_id_explicit(
    service: EmployeeRecordService,
    capturing_events: CapturingEventService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """create_employee_record passes explicit correlation_id through to the envelope."""
    async with session_factory() as session:
        await service.create_employee_record(
            session,
            external_id='corr-e',
            application_id=application_id,
            correlation_id='trace-xyz-456',
        )
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.employee_record.created')
    assert len(envelopes) == 1
    assert envelopes[0].correlation_id == 'trace-xyz-456'


@pytest.mark.asyncio
async def test_create_employee_record_correlation_id_autogenerated(
    service: EmployeeRecordService,
    capturing_events: CapturingEventService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """create_employee_record autogenerates a 32-hex correlation_id when none is supplied."""
    async with session_factory() as session:
        await service.create_employee_record(
            session,
            external_id='corr-a',
            application_id=application_id,
        )
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.employee_record.created')
    assert len(envelopes) == 1
    corr_id = envelopes[0].correlation_id
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


@pytest.mark.asyncio
async def test_add_attribute_correlation_id_autogenerated_independent_of_create(
    service: EmployeeRecordService,
    capturing_events: CapturingEventService,
    session_factory,
    application_id: uuid.UUID,
) -> None:
    """add_attribute autogenerates its own correlation_id independently of the create correlation_id."""
    async with session_factory() as session:
        record = await service.create_employee_record(
            session,
            external_id='corr-ind',
            application_id=application_id,
            correlation_id='A',
        )
        await session.flush()
        capturing_events.emitted.clear()
        await service.add_attribute(session, record.id, 'ck', 'cv')
        await session.commit()

    envelopes = capturing_events.filter_by_type('inventory.employee_record.attribute_added')
    assert len(envelopes) == 1
    corr_id = envelopes[0].correlation_id
    assert corr_id != 'A'
    assert isinstance(corr_id, str)
    assert len(corr_id) == 32
    assert all(c in '0123456789abcdef' for c in corr_id)


# ---------------------------------------------------------------------------
# Anti-dual-emit guard
# ---------------------------------------------------------------------------


def test_employee_record_service_has_no_log_attribute() -> None:
    """EmployeeRecordService must not carry a _log attribute (anti-dual-emit guard)."""
    service = EmployeeRecordService()
    assert getattr(service, '_log', None) is None
