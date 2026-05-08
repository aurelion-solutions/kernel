# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ThreatFactService."""

from __future__ import annotations

from types import SimpleNamespace
import uuid

from pydantic import ValidationError
import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.threat_facts.schemas import ThreatFactUpsert
from src.inventory.threat_facts.service import (
    ThreatFactAccountNotFoundError,
    ThreatFactConflictError,
    ThreatFactService,
    ThreatFactSubjectNotFoundError,
)
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
def service(event_service: EventService) -> ThreatFactService:
    return ThreatFactService(event_service=event_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_subject(session) -> uuid.UUID:
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_account(session) -> uuid.UUID:
    from src.inventory.accounts.models import Account
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    account = Account(
        application_id=app.id,
        username=f'user-{uuid.uuid4().hex[:8]}',
        status='active',
    )
    session.add(account)
    await session.flush()
    return account.id


def _make_payload(**kwargs: object) -> ThreatFactUpsert:
    defaults: dict[str, object] = {
        'risk_score': 0.5,
        'active_indicators': [],
        'failed_auth_count': 0,
    }
    defaults.update(kwargs)
    return ThreatFactUpsert(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_first_time_emits_created_event(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """First upsert returns (fact, created=True) and emits inventory.threat_fact.created."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        payload = _make_payload(
            risk_score=0.7,
            active_indicators=['account_takeover', 'impossible_travel'],
        )
        fact, created = await service.upsert_threat_fact(session, subject_id=subject_id, payload=payload)
        await session.commit()

    assert created is True
    assert fact.id is not None
    assert fact.risk_score == 0.7

    emitted = capturing_events.filter_by_type('inventory.threat_fact.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.COMPONENT
    assert envelope.actor_id == 'inventory.threat_facts'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(fact.id)
    payload_data = envelope.payload
    assert payload_data['fact_id'] == str(fact.id)
    assert payload_data['subject_id'] == str(subject_id)
    assert payload_data['account_id'] is None
    assert payload_data['risk_score'] == 0.7
    assert payload_data['active_indicators_count'] == 2
    assert 'active_indicators' not in payload_data
    assert payload_data['failed_auth_count'] == 0
    assert isinstance(payload_data['observed_at'], str)


@pytest.mark.asyncio
async def test_upsert_second_time_emits_updated_event(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Second upsert for same subject returns created=False and emits inventory.threat_fact.updated."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        payload1 = _make_payload(risk_score=0.3)
        fact1, created1 = await service.upsert_threat_fact(session, subject_id=subject_id, payload=payload1)
        await session.commit()
        fact1_id = fact1.id

    async with session_factory() as session:
        payload2 = _make_payload(risk_score=0.9)
        fact2, created2 = await service.upsert_threat_fact(session, subject_id=subject_id, payload=payload2)
        await session.commit()

    assert created1 is True
    assert created2 is False
    assert fact2.id == fact1_id
    assert fact2.risk_score == 0.9

    created_emitted = capturing_events.filter_by_type('inventory.threat_fact.created')
    assert len(created_emitted) == 1
    updated_emitted = capturing_events.filter_by_type('inventory.threat_fact.updated')
    assert len(updated_emitted) == 1
    assert updated_emitted[0].target_id == str(fact1_id)
    assert updated_emitted[0].payload['risk_score'] == 0.9


@pytest.mark.asyncio
async def test_upsert_dual_branch_emits_exactly_one_of_each(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create + update + update → exactly 1 .created + 2 .updated, in order."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        await service.upsert_threat_fact(session, subject_id=subject_id, payload=_make_payload(risk_score=0.1))
        await session.commit()

    async with session_factory() as session:
        await service.upsert_threat_fact(session, subject_id=subject_id, payload=_make_payload(risk_score=0.2))
        await session.commit()

    async with session_factory() as session:
        await service.upsert_threat_fact(session, subject_id=subject_id, payload=_make_payload(risk_score=0.3))
        await session.commit()

    created_emitted = capturing_events.filter_by_type('inventory.threat_fact.created')
    updated_emitted = capturing_events.filter_by_type('inventory.threat_fact.updated')
    assert len(created_emitted) == 1
    assert len(updated_emitted) == 2

    # verify ordering: .created before .updated
    all_emitted = capturing_events.emitted
    types = [e.event_type for e in all_emitted]
    assert types == [
        'inventory.threat_fact.created',
        'inventory.threat_fact.updated',
        'inventory.threat_fact.updated',
    ]


@pytest.mark.asyncio
async def test_upsert_unknown_subject_raises_and_does_not_emit(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Non-existent subject_id raises ThreatFactSubjectNotFoundError; no event emitted."""
    async with session_factory() as session:
        with pytest.raises(ThreatFactSubjectNotFoundError):
            await service.upsert_threat_fact(
                session,
                subject_id=uuid.uuid4(),
                payload=_make_payload(),
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_upsert_unknown_account_raises_and_does_not_emit(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Valid subject but bogus account_id raises ThreatFactAccountNotFoundError; no event emitted."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        payload = _make_payload(account_id=uuid.uuid4())
        with pytest.raises(ThreatFactAccountNotFoundError):
            await service.upsert_threat_fact(session, subject_id=subject_id, payload=payload)

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_upsert_conflict_raises_and_does_not_emit(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repo_upsert raises IntegrityError(pgcode=23505) → ThreatFactConflictError; no event emitted."""
    orig = SimpleNamespace(pgcode='23505', sqlstate='23505')
    fake_exc = IntegrityError('stmt', {}, orig)

    async def _raise(*args, **kwargs):  # noqa: ARG001
        raise fake_exc

    monkeypatch.setattr('src.inventory.threat_facts.service.repo_upsert', _raise)

    async with session_factory() as session:
        subject_id = await _make_subject(session)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ThreatFactConflictError):
            await service.upsert_threat_fact(
                session,
                subject_id=subject_id,
                payload=_make_payload(),
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_upsert_rejects_risk_score_out_of_range_via_schema() -> None:
    """Schema-level: ThreatFactUpsert(risk_score=1.5) raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        ThreatFactUpsert(risk_score=1.5, active_indicators=[], failed_auth_count=0)
    errors = exc_info.value.errors()
    assert any('risk_score' in str(e) for e in errors)


@pytest.mark.asyncio
async def test_get_threat_fact_does_not_emit_event(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_threat_fact returns fact without emitting any event (Q1 — read-side audit dropped)."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        fact, _ = await service.upsert_threat_fact(
            session,
            subject_id=subject_id,
            payload=_make_payload(risk_score=0.4),
        )
        await session.commit()
        fact_id = fact.id

    capturing_events.clear()

    async with session_factory() as session:
        found = await service.get_threat_fact(session, fact_id)

    assert found is not None
    assert found.id == fact_id
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_get_threat_fact_missing_returns_none_no_emit(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_threat_fact returns None and does not emit when fact is not found."""
    async with session_factory() as session:
        result = await service.get_threat_fact(session, uuid.uuid4())

    assert result is None
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_upsert_propagates_correlation_id(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_threat_fact propagates an explicit correlation_id into the envelope."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        await service.upsert_threat_fact(
            session,
            subject_id=subject_id,
            payload=_make_payload(),
            correlation_id='trace-threat-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.threat_fact.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-threat-xyz'


@pytest.mark.asyncio
async def test_upsert_generates_correlation_id_when_omitted(
    service: ThreatFactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_threat_fact generates a uuid4 hex correlation_id when caller omits it."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        await service.upsert_threat_fact(
            session,
            subject_id=subject_id,
            payload=_make_payload(),
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.threat_fact.created')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32
    assert all(c in '0123456789abcdef' for c in cid)
