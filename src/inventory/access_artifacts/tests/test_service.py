# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessArtifactService."""

from __future__ import annotations

import uuid

import pytest
from src.inventory.access_artifacts.service import (
    AccessArtifactApplicationNotFoundError,
    AccessArtifactService,
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
def service(event_service: EventService) -> AccessArtifactService:
    return AccessArtifactService(event_service=event_service)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_application_id(session) -> uuid.UUID:
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_artifact_happy_path(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_artifact creates artifact and emits inventory.access_artifact.created."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact = await service.create_artifact(
            session,
            application_id=app_id,
            source_kind='sap_role',
            external_id='role-admin',
            payload={'name': 'ADMIN'},
            ingest_batch_id='batch-001',
        )
        await session.commit()

    assert artifact.id is not None
    assert artifact.source_kind == 'sap_role'
    assert artifact.external_id == 'role-admin'
    assert artifact.payload == {'name': 'ADMIN'}

    emitted = capturing_events.filter_by_type('inventory.access_artifact.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.access_artifacts'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(artifact.id)
    assert envelope.payload['artifact_id'] == str(artifact.id)
    assert envelope.payload['application_id'] == str(app_id)
    assert envelope.payload['source_kind'] == 'sap_role'
    assert envelope.payload['external_id'] == 'role-admin'
    assert envelope.payload['ingest_batch_id'] == 'batch-001'


@pytest.mark.asyncio
async def test_create_artifact_bad_application_id(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_artifact raises AccessArtifactApplicationNotFoundError for unknown application."""
    with pytest.raises(AccessArtifactApplicationNotFoundError):
        async with session_factory() as session:
            await service.create_artifact(
                session,
                application_id=uuid.uuid4(),
                source_kind='acl_entry',
                external_id='acl-001',
                payload={'permission': 'read'},
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_get_artifact_does_not_emit_event(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_artifact returns artifact without emitting any event (Q1 — read-side audit dropped)."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact = await service.create_artifact(
            session,
            application_id=app_id,
            source_kind='db_grant',
            external_id='grant-select',
            payload={'privilege': 'SELECT'},
        )
        await session.commit()
        artifact_id = artifact.id

    # Reset captured events so only get_artifact's effect is observed
    capturing_events.clear()

    async with session_factory() as session:
        found = await service.get_artifact(session, artifact_id)

    assert found is not None
    assert found.id == artifact_id
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_get_artifact_missing(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """get_artifact returns None for unknown id, no event emitted."""
    async with session_factory() as session:
        result = await service.get_artifact(session, uuid.uuid4())

    assert result is None
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_create_artifact_propagates_correlation_id(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_artifact propagates an explicit correlation_id into the envelope."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.create_artifact(
            session,
            application_id=app_id,
            source_kind='sap_role',
            external_id='role-admin',
            payload={'name': 'ADMIN'},
            correlation_id='trace-artifact-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_artifact.created')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-artifact-xyz'


@pytest.mark.asyncio
async def test_create_artifact_generates_correlation_id_when_omitted(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_artifact generates a uuid4 hex correlation_id when caller omits it."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.create_artifact(
            session,
            application_id=app_id,
            source_kind='sap_role',
            external_id='role-admin',
            payload={'name': 'ADMIN'},
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_artifact.created')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32  # uuid4().hex = 32 hex chars
    assert cid.isalnum()
