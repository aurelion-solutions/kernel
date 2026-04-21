# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ArtifactBindingService."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.artifact_bindings.service import (
    ArtifactBindingForeignKeyError,
    ArtifactBindingService,
    ArtifactBindingTargetRequiredError,
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
def service(event_service: EventService) -> ArtifactBindingService:
    return ArtifactBindingService(event_service=event_service)


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
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_binding creates binding with all targets and emits inventory.artifact_binding.created."""
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

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.artifact_bindings'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(binding.id)
    assert envelope.payload['binding_id'] == str(binding.id)
    assert envelope.payload['artifact_id'] == str(ids['artifact_id'])
    assert envelope.payload['access_fact_id'] == str(ids['access_fact_id'])
    assert envelope.payload['resource_id'] == str(ids['resource_id'])
    assert envelope.payload['account_id'] == str(ids['account_id'])


@pytest.mark.asyncio
async def test_create_binding_no_target_raises(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
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

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_binding_bad_artifact_id(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
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

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_get_binding_does_not_emit_event(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_binding returns binding without emitting any event (Q1 — read-side audit dropped)."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        binding = await service.create_binding(
            session,
            artifact_id=ids['artifact_id'],
            access_fact_id=ids['access_fact_id'],
        )
        await session.commit()
        binding_id = binding.id

    # Reset captured events so only get_binding's effect is observed
    capturing_events.clear()

    async with session_factory() as session:
        found = await service.get_binding(session, binding_id)

    assert found is not None
    assert found.id == binding_id
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_binding_propagates_correlation_id(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_binding propagates an explicit correlation_id into the envelope."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_binding(
            session,
            artifact_id=ids['artifact_id'],
            access_fact_id=ids['access_fact_id'],
            correlation_id='trace-binding-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-binding-xyz'


@pytest.mark.asyncio
async def test_create_binding_generates_correlation_id_when_omitted(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_binding generates a uuid4 hex correlation_id when caller omits it."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await service.create_binding(
            session,
            artifact_id=ids['artifact_id'],
            access_fact_id=ids['access_fact_id'],
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32  # uuid4().hex = 32 hex chars
    assert cid.isalnum()
