# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessFactService."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.access_facts.service import (
    AccessFactForeignKeyError,
    AccessFactService,
    DuplicateAccessFactError,
)
from src.inventory.enums import Action
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
def service(log_service: LogService) -> AccessFactService:
    return AccessFactService(log_service=log_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_prerequisites(session) -> dict:
    """Create employee, subject, resource. Return dict with ids."""
    from src.inventory.employees.repository import create_employee
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

    return {
        'subject_id': subj.id,
        'resource_id': resource.id,
        'account_id': None,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_fact_happy_path(
    service: AccessFactService,
    session_factory,
    log_path: Path,
) -> None:
    """create_fact creates fact and emits access_fact.created event."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        await session.commit()

    assert fact.id is not None
    assert fact.subject_id == ids['subject_id']
    assert fact.resource_id == ids['resource_id']
    assert fact.action == Action.read
    assert fact.effect == AccessFactEffect.allow

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'access_fact.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'inventory.access_facts'
    assert 'access_fact_id' in created[-1]['payload']
    assert created[-1]['payload']['subject_id'] == str(ids['subject_id'])
    assert created[-1]['payload']['resource_id'] == str(ids['resource_id'])
    assert created[-1]['payload']['action'] == 'read'
    assert created[-1]['payload']['effect'] == 'allow'


@pytest.mark.asyncio
async def test_create_fact_duplicate(
    service: AccessFactService,
    session_factory,
) -> None:
    """create_fact raises DuplicateAccessFactError on duplicate natural key."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.write,
            effect=AccessFactEffect.allow,
        )
        await session.commit()

    with pytest.raises(DuplicateAccessFactError):
        async with session_factory() as session:
            # Use same IDs to force uniqueness violation
            await service.create_fact(
                session,
                subject_id=ids['subject_id'],
                account_id=None,
                resource_id=ids['resource_id'],
                action=Action.write,
                effect=AccessFactEffect.allow,
            )


@pytest.mark.asyncio
async def test_create_fact_bad_subject_id(
    service: AccessFactService,
    session_factory,
) -> None:
    """create_fact raises AccessFactForeignKeyError for unknown subject_id."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(AccessFactForeignKeyError):
            await service.create_fact(
                session,
                subject_id=uuid.uuid4(),
                account_id=None,
                resource_id=ids['resource_id'],
                action=Action.read,
                effect=AccessFactEffect.allow,
            )


@pytest.mark.asyncio
async def test_get_fact_found(
    service: AccessFactService,
    session_factory,
    log_path: Path,
) -> None:
    """get_fact returns fact and emits access_fact.retrieved."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.execute,
            effect=AccessFactEffect.allow,
        )
        await session.commit()
        fact_id = fact.id

    async with session_factory() as session:
        found = await service.get_fact(session, fact_id)

    assert found is not None
    assert found.id == fact_id

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'access_fact.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'inventory.access_facts'


@pytest.mark.asyncio
async def test_invalidate_fact(
    service: AccessFactService,
    session_factory,
    log_path: Path,
) -> None:
    """invalidate_fact sets valid_until and emits access_fact.invalidated at WARNING."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        fact = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.approve,
            effect=AccessFactEffect.deny,
        )
        await session.commit()
        fact_id = fact.id

    async with session_factory() as session:
        updated = await service.invalidate_fact(session, fact_id)
        await session.commit()

    assert updated.valid_until is not None

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    invalidated = [r for r in records if r.get('event_type') == 'access_fact.invalidated']
    assert len(invalidated) >= 1
    last = invalidated[-1]
    assert last['component'] == 'inventory.access_facts'
    assert last['level'] == 'warning'
    assert 'access_fact_id' in last['payload']
    assert 'at' in last['payload']


@pytest.mark.asyncio
async def test_get_fact_by_natural_key_returns_fact_with_null_account(
    service: AccessFactService,
    session_factory,
) -> None:
    """get_fact_by_natural_key returns the fact when account_id is None (exercises NULLS NOT DISTINCT)."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        created = await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        await session.commit()
        fact_id = created.id

    async with session_factory() as session:
        found = await service.get_fact_by_natural_key(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )

    assert found is not None
    assert found.id == fact_id
    assert found.account_id is None


@pytest.mark.asyncio
async def test_get_fact_by_natural_key_returns_none_when_absent(
    service: AccessFactService,
    session_factory,
) -> None:
    """get_fact_by_natural_key returns None when no matching fact exists."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await session.commit()

    async with session_factory() as session:
        found = await service.get_fact_by_natural_key(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.administer,
            effect=AccessFactEffect.deny,
        )

    assert found is None


@pytest.mark.asyncio
async def test_create_fact_on_duplicate_does_not_rollback_outer_transaction(
    service: AccessFactService,
    session_factory,
) -> None:
    """create_fact no longer rolls back outer transaction on DuplicateAccessFactError.

    Regression guard: if session.rollback() is ever re-introduced inside create_fact,
    the Resource written before the duplicate call would disappear and this test fails.
    """
    from src.inventory.resources.models import Resource

    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        # Write first fact to establish the natural key.
        await service.create_fact(
            session,
            subject_id=ids['subject_id'],
            account_id=None,
            resource_id=ids['resource_id'],
            action=Action.read,
            effect=AccessFactEffect.allow,
        )
        await session.commit()

    async with session_factory() as session:
        # Reload resource — it must survive regardless of what happens next.
        resource = await session.get(Resource, ids['resource_id'])
        assert resource is not None

        # Attempt duplicate inside a savepoint so the outer transaction stays open.
        with pytest.raises(DuplicateAccessFactError):
            async with session.begin_nested():
                await service.create_fact(
                    session,
                    subject_id=ids['subject_id'],
                    account_id=None,
                    resource_id=ids['resource_id'],
                    action=Action.read,
                    effect=AccessFactEffect.allow,
                )

        # The resource must still be accessible in the same session after the error —
        # proof that the outer transaction was NOT rolled back.
        still_alive = await session.get(Resource, ids['resource_id'])
        assert still_alive is not None
        assert still_alive.id == ids['resource_id']
