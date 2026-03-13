# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessUsageFactService."""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import uuid

import pytest
from src.inventory.access_usage_facts.service import (
    AccessUsageFactDuplicateError,
    AccessUsageFactForeignKeyError,
    AccessUsageFactService,
    AccessUsageFactWindowOrderError,
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
def service(log_service: LogService) -> AccessUsageFactService:
    return AccessUsageFactService(log_service=log_service)


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


async def _make_resource(session) -> uuid.UUID:
    from src.inventory.resources.models import Resource
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
    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app.id,
        kind='database',
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _make_access_fact(session) -> uuid.UUID:
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.enums import Action

    subject_id = await _make_subject(session)
    resource_id = await _make_resource(session)
    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
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
async def test_create_usage_fact_happy_path_closed_window(
    service: AccessUsageFactService,
    session_factory,
    log_path: Path,
) -> None:
    """create_usage_fact with window_to set emits access_usage_fact.created INFO event."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        w_to = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        last_seen = datetime(2026, 1, 1, 9, 45, 0, tzinfo=UTC)

        usage_fact = await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=last_seen,
            usage_count=7,
            window_from=w_from,
            window_to=w_to,
        )
        await session.commit()

    assert usage_fact.id is not None
    assert usage_fact.usage_count == 7

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'access_usage_fact.created']
    assert len(created) == 1
    assert created[0]['component'] == 'inventory.access_usage_facts'
    payload = created[0]['payload']
    assert 'usage_fact_id' in payload
    assert 'access_fact_id' in payload
    assert 'last_seen' in payload
    assert 'window_from' in payload
    assert payload['window_to'] is not None


@pytest.mark.asyncio
async def test_create_usage_fact_happy_path_open_window(
    service: AccessUsageFactService,
    session_factory,
    log_path: Path,
) -> None:
    """create_usage_fact with window_to=None (open window) emits event with null window_to."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        last_seen = datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC)

        usage_fact = await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=last_seen,
            usage_count=2,
            window_from=w_from,
            window_to=None,
        )
        await session.commit()

    assert usage_fact.window_to is None

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'access_usage_fact.created']
    assert len(created) == 1
    assert created[0]['payload']['window_to'] is None


@pytest.mark.asyncio
async def test_create_usage_fact_unknown_access_fact_raises_422(
    service: AccessUsageFactService,
    session_factory,
    log_path: Path,
) -> None:
    """Non-existent access_fact_id raises AccessUsageFactForeignKeyError; no event emitted."""
    async with session_factory() as session:
        with pytest.raises(AccessUsageFactForeignKeyError):
            await service.create_usage_fact(
                session,
                access_fact_id=uuid.uuid4(),
                last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
                usage_count=1,
                window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
                window_to=None,
            )

    assert not log_path.exists() or log_path.read_text().strip() == ''


@pytest.mark.asyncio
async def test_create_usage_fact_rejects_inverted_window(
    service: AccessUsageFactService,
    session_factory,
    log_path: Path,
) -> None:
    """window_to <= window_from raises AccessUsageFactWindowOrderError before any DB round-trip."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        w_to = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)  # earlier than w_from

        with pytest.raises(AccessUsageFactWindowOrderError):
            await service.create_usage_fact(
                session,
                access_fact_id=access_fact_id,
                last_seen=datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC),
                usage_count=1,
                window_from=w_from,
                window_to=w_to,
            )

    assert not log_path.exists() or log_path.read_text().strip() == ''


@pytest.mark.asyncio
async def test_create_usage_fact_duplicate_window_raises_409(
    service: AccessUsageFactService,
    session_factory,
    log_path: Path,
) -> None:
    """Duplicate (access_fact_id, window_from, window_to) raises AccessUsageFactDuplicateError.

    Also verifies NULLS NOT DISTINCT: two rows with window_to=None are rejected.
    """
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        w_from = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        w_to = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        last_seen = datetime(2026, 1, 1, 9, 30, 0, tzinfo=UTC)

        # First insert (closed window) — must succeed
        await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=last_seen,
            usage_count=1,
            window_from=w_from,
            window_to=w_to,
        )
        await session.commit()

    async with session_factory() as session:
        # Second insert with same closed window — must fail
        with pytest.raises(AccessUsageFactDuplicateError):
            await service.create_usage_fact(
                session,
                access_fact_id=access_fact_id,
                last_seen=last_seen,
                usage_count=2,
                window_from=w_from,
                window_to=w_to,
            )

    # NULLS NOT DISTINCT: open-ended window — first insert must succeed
    async with session_factory() as session:
        access_fact_id2 = await _make_access_fact(session)
        w_from2 = datetime(2026, 2, 1, 9, 0, 0, tzinfo=UTC)
        await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id2,
            last_seen=datetime(2026, 2, 1, 9, 30, 0, tzinfo=UTC),
            usage_count=1,
            window_from=w_from2,
            window_to=None,
        )
        await session.commit()

    async with session_factory() as session:
        # Second insert with same open window (NULL) — must also fail due to NULLS NOT DISTINCT
        with pytest.raises(AccessUsageFactDuplicateError):
            await service.create_usage_fact(
                session,
                access_fact_id=access_fact_id2,
                last_seen=datetime(2026, 2, 1, 9, 45, 0, tzinfo=UTC),
                usage_count=2,
                window_from=w_from2,
                window_to=None,
            )


@pytest.mark.asyncio
async def test_get_usage_fact_emits_retrieved_event(
    service: AccessUsageFactService,
    session_factory,
    log_path: Path,
) -> None:
    """get_usage_fact emits access_usage_fact.retrieved INFO event when found."""
    async with session_factory() as session:
        access_fact_id = await _make_access_fact(session)
        usage_fact = await service.create_usage_fact(
            session,
            access_fact_id=access_fact_id,
            last_seen=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            usage_count=3,
            window_from=datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC),
            window_to=None,
        )
        await session.commit()
        usage_fact_id = usage_fact.id

    async with session_factory() as session:
        found = await service.get_usage_fact(session, usage_fact_id)

    assert found is not None

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'access_usage_fact.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['payload']['usage_fact_id'] == str(usage_fact_id)
