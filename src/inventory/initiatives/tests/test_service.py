# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for InitiativeService."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import uuid

import pytest
from src.inventory.initiatives.models import InitiativeType
from src.inventory.initiatives.schemas import InitiativePatch
from src.inventory.initiatives.service import (
    InitiativeEmptyPatchError,
    InitiativeForeignKeyError,
    InitiativeService,
)
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / 'logs.jsonl'


@pytest.fixture
def log_service(log_path: Path) -> LogService:
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    return LogService(factory=factory, provider_name='file')


@pytest.fixture
def service(log_service: LogService) -> InitiativeService:
    return InitiativeService(log_service=log_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_access_fact(session) -> uuid.UUID:
    """Create an access fact, return fact.id."""
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.employees.repository import create_employee
    from src.inventory.enums import Action
    from src.inventory.persons.repository import create_person
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind
    from src.platform.applications.models import Application

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
    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app.id,
        kind='database',
    )
    session.add(resource)
    await session.flush()
    fact = AccessFact(
        subject_id=subj.id,
        resource_id=resource.id,
        action=Action.read,
        effect=AccessFactEffect.allow,
    )
    session.add(fact)
    await session.flush()
    return fact.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('initiative_type', list(InitiativeType))
async def test_create_initiative_happy_path(
    initiative_type: InitiativeType,
    service: InitiativeService,
    session_factory,
    log_path: Path,
) -> None:
    """create_initiative succeeds for all 9 types and emits initiative.created INFO event."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=initiative_type,
            origin=f'test origin for {initiative_type.value}',
        )
        await session.commit()

    assert initiative.id is not None
    assert initiative.access_fact_id == fact_id
    assert initiative.type == initiative_type

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    created_events = [r for r in records if r.get('event_type') == 'initiative.created']
    assert len(created_events) >= 1
    last = created_events[-1]
    assert last['level'].upper() == 'INFO'
    assert last['component'] == 'inventory.initiatives'
    assert 'initiative_id' in last['payload']
    assert last['payload']['access_fact_id'] == str(fact_id)
    assert last['payload']['type'] == initiative_type.value
    assert 'origin' in last['payload']


@pytest.mark.asyncio
async def test_create_initiative_bad_access_fact_raises(
    service: InitiativeService,
    session_factory,
    log_path: Path,
) -> None:
    """create_initiative raises InitiativeForeignKeyError for unknown access_fact_id."""
    async with session_factory() as session:
        with pytest.raises(InitiativeForeignKeyError):
            await service.create_initiative(
                session,
                access_fact_id=uuid.uuid4(),
                type_=InitiativeType.birthright,
                origin='should fail',
            )

    # No events should have been emitted
    assert not log_path.exists() or log_path.read_text().strip() == ''


@pytest.mark.asyncio
async def test_update_initiative_origin_only(
    service: InitiativeService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH origin only emits initiative.updated; no initiative.expired emitted."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.requested,
            origin='original origin',
        )
        await session.commit()
        initiative_id = initiative.id

    async with session_factory() as session:
        patch = InitiativePatch(origin='updated origin')
        updated = await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert updated.origin == 'updated origin'

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    updated_events = [r for r in records if r.get('event_type') == 'initiative.updated']
    expired_events = [r for r in records if r.get('event_type') == 'initiative.expired']

    assert len(updated_events) >= 1
    assert updated_events[-1]['payload']['changed_fields'] == ['origin']
    assert len(expired_events) == 0


@pytest.mark.asyncio
async def test_update_initiative_sets_valid_until_in_past_emits_expired(
    service: InitiativeService,
    session_factory,
    log_path: Path,
) -> None:
    """PATCH valid_until to past emits both initiative.updated and initiative.expired (WARNING)."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.trial,
            origin='trial period',
        )
        await session.commit()
        initiative_id = initiative.id

    past_dt = datetime.now(UTC) - timedelta(minutes=1)
    async with session_factory() as session:
        patch = InitiativePatch(valid_until=past_dt)
        updated = await service.update_initiative(session, initiative_id, patch)
        await session.commit()

    assert updated.valid_until is not None

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    updated_events = [r for r in records if r.get('event_type') == 'initiative.updated']
    expired_events = [r for r in records if r.get('event_type') == 'initiative.expired']

    assert len(updated_events) >= 1
    assert len(expired_events) >= 1
    assert expired_events[-1]['level'].upper() == 'WARNING'
    assert 'at' in expired_events[-1]['payload']


@pytest.mark.asyncio
async def test_update_initiative_empty_patch_raises(
    service: InitiativeService,
    session_factory,
    log_path: Path,
) -> None:
    """Empty InitiativePatch raises InitiativeEmptyPatchError; no event emitted."""
    async with session_factory() as session:
        fact_id = await _make_access_fact(session)
        initiative = await service.create_initiative(
            session,
            access_fact_id=fact_id,
            type_=InitiativeType.inherited,
            origin='some origin',
        )
        await session.commit()
        initiative_id = initiative.id

    async with session_factory() as session:
        patch = InitiativePatch()
        with pytest.raises(InitiativeEmptyPatchError):
            await service.update_initiative(session, initiative_id, patch)
