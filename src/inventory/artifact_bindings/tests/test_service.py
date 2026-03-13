# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ArtifactBindingService."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.artifact_bindings.service import (
    ArtifactBindingForeignKeyError,
    ArtifactBindingService,
    ArtifactBindingTargetRequiredError,
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
def service(log_service: LogService) -> ArtifactBindingService:
    return ArtifactBindingService(log_service=log_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_prerequisites(session) -> dict:
    """Create all required entities, return dict with ids."""
    from src.inventory.access_artifacts.models import AccessArtifact
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.accounts.models import Account, AccountStatus
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

    account = Account(
        application_id=app.id,
        username=f'user-{uuid.uuid4().hex[:8]}',
        status=AccountStatus.active,
        meta={},
    )
    session.add(account)
    await session.flush()

    artifact = AccessArtifact(
        application_id=app.id,
        source_kind='acl_entry',
        external_id=str(uuid.uuid4()),
        payload={'raw': 'data'},
    )
    session.add(artifact)
    await session.flush()

    fact = AccessFact(
        subject_id=subj.id,
        resource_id=resource.id,
        action=Action.read,
        effect=AccessFactEffect.allow,
    )
    session.add(fact)
    await session.flush()

    return {
        'artifact_id': artifact.id,
        'access_fact_id': fact.id,
        'resource_id': resource.id,
        'account_id': account.id,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_binding_happy_path(
    service: ArtifactBindingService,
    session_factory,
    log_path: Path,
) -> None:
    """create_binding creates binding with all targets and emits artifact_binding.created."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        binding = await service.create_binding(
            session,
            artifact_id=ids['artifact_id'],
            access_fact_id=ids['access_fact_id'],
            resource_id=ids['resource_id'],
            account_id=ids['account_id'],
        )
        await session.commit()

    assert binding.id is not None
    assert binding.artifact_id == ids['artifact_id']
    assert binding.access_fact_id == ids['access_fact_id']
    assert binding.resource_id == ids['resource_id']
    assert binding.account_id == ids['account_id']

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'artifact_binding.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'inventory.artifact_bindings'
    assert 'binding_id' in created[-1]['payload']
    assert created[-1]['payload']['artifact_id'] == str(ids['artifact_id'])


@pytest.mark.asyncio
async def test_create_binding_no_target_raises(
    service: ArtifactBindingService,
    session_factory,
) -> None:
    """create_binding raises ArtifactBindingTargetRequiredError when all targets are None."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(ArtifactBindingTargetRequiredError):
            await service.create_binding(
                session,
                artifact_id=ids['artifact_id'],
                access_fact_id=None,
                resource_id=None,
                account_id=None,
            )


@pytest.mark.asyncio
async def test_create_binding_bad_artifact_id(
    service: ArtifactBindingService,
    session_factory,
) -> None:
    """create_binding raises ArtifactBindingForeignKeyError for unknown artifact_id."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        with pytest.raises(ArtifactBindingForeignKeyError):
            await service.create_binding(
                session,
                artifact_id=uuid.uuid4(),  # non-existent
                access_fact_id=ids['access_fact_id'],
            )


@pytest.mark.asyncio
async def test_get_binding_found(
    service: ArtifactBindingService,
    session_factory,
    log_path: Path,
) -> None:
    """get_binding returns binding and emits artifact_binding.retrieved."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        binding = await service.create_binding(
            session,
            artifact_id=ids['artifact_id'],
            access_fact_id=ids['access_fact_id'],
        )
        await session.commit()
        binding_id = binding.id

    async with session_factory() as session:
        found = await service.get_binding(session, binding_id)

    assert found is not None
    assert found.id == binding_id

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'artifact_binding.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'inventory.artifact_bindings'
