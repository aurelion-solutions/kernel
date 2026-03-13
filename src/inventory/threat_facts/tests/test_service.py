# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ThreatFactService."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

from pydantic import ValidationError
import pytest
from src.inventory.threat_facts.schemas import ThreatFactUpsert
from src.inventory.threat_facts.service import (
    ThreatFactAccountNotFoundError,
    ThreatFactService,
    ThreatFactSubjectNotFoundError,
)
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / 'logs.jsonl'


@pytest.fixture
def log_service(log_path: Path) -> LogService:
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    return LogService(factory=factory, provider_name='file')


@pytest.fixture
def service(log_service: LogService) -> ThreatFactService:
    return ThreatFactService(log_service=log_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_subject(session) -> uuid.UUID:
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject, SubjectKind

    person = await create_person(session, external_id=str(uuid.uuid4()), description='test')
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
    session_factory,
    log_path: Path,
) -> None:
    """First upsert returns (fact, created=True) and emits threat_fact.created."""
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created_events = [r for r in records if r.get('event_type') == 'threat_fact.created']
    assert len(created_events) == 1
    assert created_events[0]['component'] == 'inventory.threat_facts'
    payload_data = created_events[0]['payload']
    assert 'active_indicators_count' in payload_data
    assert isinstance(payload_data['active_indicators_count'], int)
    assert payload_data['active_indicators_count'] == 2
    assert 'active_indicators' not in payload_data


@pytest.mark.asyncio
async def test_upsert_second_time_emits_updated_event(
    service: ThreatFactService,
    session_factory,
    log_path: Path,
) -> None:
    """Second upsert for same subject returns created=False and emits threat_fact.updated."""
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    updated_events = [r for r in records if r.get('event_type') == 'threat_fact.updated']
    assert len(updated_events) == 1
    created_events = [r for r in records if r.get('event_type') == 'threat_fact.created']
    assert len(created_events) == 1


@pytest.mark.asyncio
async def test_upsert_unknown_subject_raises_422(
    service: ThreatFactService,
    session_factory,
    log_path: Path,
) -> None:
    """Non-existent subject_id raises ThreatFactSubjectNotFoundError; no event emitted."""
    async with session_factory() as session:
        with pytest.raises(ThreatFactSubjectNotFoundError):
            await service.upsert_threat_fact(
                session,
                subject_id=uuid.uuid4(),
                payload=_make_payload(),
            )

    assert not log_path.exists() or log_path.read_text().strip() == ''


@pytest.mark.asyncio
async def test_upsert_unknown_account_raises_422(
    service: ThreatFactService,
    session_factory,
    log_path: Path,
) -> None:
    """Valid subject but bogus account_id raises ThreatFactAccountNotFoundError; no event."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        payload = _make_payload(account_id=uuid.uuid4())
        with pytest.raises(ThreatFactAccountNotFoundError):
            await service.upsert_threat_fact(session, subject_id=subject_id, payload=payload)

    assert not log_path.exists() or log_path.read_text().strip() == ''


@pytest.mark.asyncio
async def test_upsert_rejects_risk_score_out_of_range_via_schema() -> None:
    """Schema-level: ThreatFactUpsert(risk_score=1.5) raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        ThreatFactUpsert(risk_score=1.5, active_indicators=[], failed_auth_count=0)
    errors = exc_info.value.errors()
    assert any('risk_score' in str(e) for e in errors)


@pytest.mark.asyncio
async def test_get_threat_fact_emits_retrieved_event(
    service: ThreatFactService,
    session_factory,
    log_path: Path,
) -> None:
    """get_threat_fact emits threat_fact.retrieved INFO event when found."""
    async with session_factory() as session:
        subject_id = await _make_subject(session)
        fact, _ = await service.upsert_threat_fact(
            session,
            subject_id=subject_id,
            payload=_make_payload(risk_score=0.4),
        )
        await session.commit()
        fact_id = fact.id

    async with session_factory() as session:
        found = await service.get_threat_fact(session, fact_id)

    assert found is not None

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'threat_fact.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['payload']['fact_id'] == str(fact_id)
