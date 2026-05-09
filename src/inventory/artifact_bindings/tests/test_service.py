# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ArtifactBindingService — polymorphic (target_type, target_id) shape."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.artifact_bindings.service import (
    ArtifactBindingArtifactNotFoundError,
    ArtifactBindingDuplicateError,
    ArtifactBindingService,
    ArtifactBindingUnknownTargetTypeError,
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
# Entity builders
# ---------------------------------------------------------------------------


async def _make_application(session) -> uuid.UUID:
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
    return app.id


async def _make_artifact(session, app_id: uuid.UUID) -> uuid.UUID:
    from datetime import UTC, datetime

    import sqlalchemy as sa

    artifact_id = uuid.uuid4()
    await session.execute(
        sa.text(
            'INSERT INTO access_artifacts '
            '(id, application_id, artifact_type, external_id, payload, observed_at) '
            'VALUES (:id, :application_id, :artifact_type, :external_id, CAST(:payload AS jsonb), :observed_at)'
        ),
        {
            'id': artifact_id,
            'application_id': app_id,
            'artifact_type': 'acl_entry',
            'external_id': str(uuid.uuid4()),
            'payload': '{"raw": "data"}',
            'observed_at': datetime(2026, 1, 1, tzinfo=UTC),
        },
    )
    await session.flush()
    return artifact_id


async def _make_resource(session, app_id: uuid.UUID) -> uuid.UUID:
    from src.inventory.resources.models import Resource

    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app_id,
        kind='database',
        resource_type='database',
        resource_key=str(uuid.uuid4()),
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _make_account(session, app_id: uuid.UUID) -> uuid.UUID:
    from src.inventory.accounts.models import Account, AccountStatus

    account = Account(
        application_id=app_id,
        username=f'user-{uuid.uuid4().hex[:8]}',
        status=AccountStatus.active,
        meta={},
    )
    session.add(account)
    await session.flush()
    return account.id


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


async def _make_access_fact(session, subject_id: uuid.UUID, resource_id: uuid.UUID) -> uuid.UUID:
    """Synthesize an access_fact UUID.

    Phase 15 Step 16: PG ``access_facts`` table was dropped — facts now live in
    Iceberg. The artifact binding ``target_id`` for kind=``access_fact`` is a
    plain UUID with no FK, so we just return a fresh id.
    """
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Tests — create_binding per target type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_binding_target_access_fact(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_binding with target_type='access_fact' persists row and emits event."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        resource_id = await _make_resource(session, app_id)
        subject_id = await _make_subject(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)

        binding = await service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='access_fact',
            target_id=fact_id,
        )
        await session.commit()

    assert binding.id is not None
    assert binding.artifact_id == artifact_id
    assert binding.target_type == 'access_fact'
    assert binding.target_id == fact_id

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    p = emitted[0].payload
    assert p['binding_id'] == str(binding.id)
    assert p['artifact_id'] == str(artifact_id)
    assert p['target_type'] == 'access_fact'
    assert p['target_id'] == str(fact_id)
    assert set(p.keys()) == {'binding_id', 'artifact_id', 'target_type', 'target_id'}


@pytest.mark.asyncio
async def test_create_binding_target_resource(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_binding with target_type='resource' persists row and emits event."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        resource_id = await _make_resource(session, app_id)

        binding = await service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='resource',
            target_id=resource_id,
        )
        await session.commit()

    assert binding.target_type == 'resource'
    assert binding.target_id == resource_id

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    assert emitted[0].payload['target_type'] == 'resource'
    assert emitted[0].payload['target_id'] == str(resource_id)


@pytest.mark.asyncio
async def test_create_binding_target_account(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_binding with target_type='account' persists row and emits event."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        account_id = await _make_account(session, app_id)

        binding = await service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='account',
            target_id=account_id,
        )
        await session.commit()

    assert binding.target_type == 'account'
    assert binding.target_id == account_id

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    assert emitted[0].payload['target_type'] == 'account'


@pytest.mark.asyncio
async def test_create_binding_target_subject(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_binding with target_type='subject' persists row and emits event."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        subject_id = await _make_subject(session)

        binding = await service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='subject',
            target_id=subject_id,
        )
        await session.commit()

    assert binding.target_type == 'subject'
    assert binding.target_id == subject_id

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    assert emitted[0].payload['target_type'] == 'subject'


# ---------------------------------------------------------------------------
# Tests — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_binding_unknown_target_type_raises(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """target_type='wat' raises ArtifactBindingUnknownTargetTypeError, no event, no row."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)

        with pytest.raises(ArtifactBindingUnknownTargetTypeError):
            await service.create_binding(
                session,
                artifact_id=artifact_id,
                target_type='wat',
                target_id=uuid.uuid4(),
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_binding_artifact_not_found_raises(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Random artifact_id raises ArtifactBindingArtifactNotFoundError, no event."""
    async with session_factory() as session:
        with pytest.raises(ArtifactBindingArtifactNotFoundError):
            await service.create_binding(
                session,
                artifact_id=uuid.uuid4(),
                target_type='resource',
                target_id=uuid.uuid4(),
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_binding_duplicate_raises(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Second call with same (artifact_id, target_type, target_id) raises ArtifactBindingDuplicateError."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        resource_id = await _make_resource(session, app_id)

        await service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='resource',
            target_id=resource_id,
        )
        await session.commit()

    # First event emitted
    assert len(capturing_events.filter_by_type('inventory.artifact_binding.created')) == 1

    # Second call — same triple → duplicate
    async with session_factory() as session:
        with pytest.raises(ArtifactBindingDuplicateError):
            await service.create_binding(
                session,
                artifact_id=artifact_id,
                target_type='resource',
                target_id=resource_id,
            )

    # Only one event total (second call raised before emit)
    assert len(capturing_events.filter_by_type('inventory.artifact_binding.created')) == 1


# ---------------------------------------------------------------------------
# Tests — correlation_id propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_binding_correlation_id_propagation(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Explicit correlation_id is propagated into the event envelope."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        resource_id = await _make_resource(session, app_id)

        await service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='resource',
            target_id=resource_id,
            correlation_id='trace-test-abc',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-test-abc'

    # Also verify actor/target shape
    assert emitted[0].actor_kind == EventParticipantKind.COMPONENT
    assert emitted[0].actor_id == 'inventory.artifact_bindings'
    assert emitted[0].target_kind == EventParticipantKind.SYSTEM


@pytest.mark.asyncio
async def test_create_binding_generates_correlation_id_when_omitted(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Omitted correlation_id generates a 32-char hex string."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        resource_id = await _make_resource(session, app_id)

        await service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='resource',
            target_id=resource_id,
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.artifact_binding.created')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32
    assert cid.isalnum()


# ---------------------------------------------------------------------------
# Tests — list_bindings filters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_bindings_filter_by_target_type(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """list_bindings(target_type='resource') returns only resource bindings."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        resource_id = await _make_resource(session, app_id)
        account_id = await _make_account(session, app_id)

        await service.create_binding(session, artifact_id=artifact_id, target_type='resource', target_id=resource_id)
        await service.create_binding(session, artifact_id=artifact_id, target_type='account', target_id=account_id)
        await session.commit()

    async with session_factory() as session:
        results = await service.list_bindings(session, target_type='resource')

    assert len(results) >= 1
    assert all(b.target_type == 'resource' for b in results)


@pytest.mark.asyncio
async def test_list_bindings_filter_by_target_id(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """list_bindings(target_id=uuid) returns only rows with that target_id."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_artifact(session, app_id)
        resource_id1 = await _make_resource(session, app_id)
        resource_id2 = await _make_resource(session, app_id)

        await service.create_binding(session, artifact_id=artifact_id, target_type='resource', target_id=resource_id1)
        await service.create_binding(session, artifact_id=artifact_id, target_type='resource', target_id=resource_id2)
        await session.commit()

    async with session_factory() as session:
        results = await service.list_bindings(session, target_id=resource_id1)

    assert len(results) >= 1
    assert all(b.target_id == resource_id1 for b in results)


@pytest.mark.asyncio
async def test_list_bindings_filter_by_target_type_and_target_id(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """list_bindings(target_type, target_id) combined filter returns exact provenance set."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id1 = await _make_artifact(session, app_id)
        artifact_id2 = await _make_artifact(session, app_id)
        resource_id = await _make_resource(session, app_id)
        account_id = await _make_account(session, app_id)

        b1 = await service.create_binding(
            session, artifact_id=artifact_id1, target_type='resource', target_id=resource_id
        )
        await service.create_binding(session, artifact_id=artifact_id2, target_type='account', target_id=account_id)
        await service.create_binding(session, artifact_id=artifact_id2, target_type='resource', target_id=resource_id)
        await session.commit()

    async with session_factory() as session:
        results = await service.list_bindings(session, target_type='resource', target_id=resource_id)

    assert len(results) >= 2
    assert all(b.target_type == 'resource' and b.target_id == resource_id for b in results)
    ids = {b.id for b in results}
    assert b1.id in ids


@pytest.mark.asyncio
async def test_list_bindings_filter_by_artifact_id(
    service: ArtifactBindingService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """list_bindings(artifact_id=...) returns only bindings for that artifact."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id1 = await _make_artifact(session, app_id)
        artifact_id2 = await _make_artifact(session, app_id)
        resource_id1 = await _make_resource(session, app_id)
        resource_id2 = await _make_resource(session, app_id)

        b1 = await service.create_binding(
            session, artifact_id=artifact_id1, target_type='resource', target_id=resource_id1
        )
        await service.create_binding(session, artifact_id=artifact_id2, target_type='resource', target_id=resource_id2)
        await session.commit()

    async with session_factory() as session:
        results = await service.list_bindings(session, artifact_id=artifact_id1)

    assert len(results) >= 1
    assert all(b.artifact_id == artifact_id1 for b in results)
    assert any(b.id == b1.id for b in results)
