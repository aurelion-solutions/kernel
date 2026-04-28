# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for ACLNormalizerService — real Postgres via session_factory fixture."""

from __future__ import annotations

import uuid

import pytest
from src.capabilities.normalization.acl.schemas import ACLEntryPayload
from src.capabilities.normalization.acl.service import ACLNormalizerService
from src.inventory.access_artifacts.service import (
    AccessArtifactApplicationNotFoundError,
    AccessArtifactService,
)
from src.inventory.access_facts.service import AccessFactService
from src.inventory.artifact_bindings.models import ArtifactBinding
from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.inventory.resources.models import Resource
from src.inventory.resources.service import ResourceService
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def log_service(tmp_path) -> LogService:
    from pathlib import Path

    log_path: Path = tmp_path / 'logs.jsonl'
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    return LogService(sink=factory.get('file'))


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def acl_service(log_service: LogService, event_service: EventService) -> ACLNormalizerService:
    return ACLNormalizerService(
        artifact_service=AccessArtifactService(event_service=event_service),
        resource_service=ResourceService(event_service=event_service),
        access_fact_service=AccessFactService(event_service=event_service),
        binding_service=ArtifactBindingService(event_service=event_service),
        log_service=log_service,
    )


def _make_payload(
    resource_external_id: str = '/repo/core/src',
    verb: str = 'read',
    effect: str = 'allow',
) -> ACLEntryPayload:
    return ACLEntryPayload(
        resource_external_id=resource_external_id,
        resource_kind='folder',
        verb=verb,  # type: ignore[arg-type]
        effect=effect,  # type: ignore[arg-type]
        environment='production',
        data_sensitivity='financial',
    )


async def _make_prerequisites(session) -> dict:
    """Create Application, Customer, Subject. Return dict with application_id, subject_id."""
    from src.inventory.customers.models import Customer
    from src.inventory.subjects.models import Subject, SubjectKind
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-acl-app-{uuid.uuid4()}',
        code=f'acl-app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    customer = Customer(
        external_id=str(uuid.uuid4()),
    )
    session.add(customer)
    await session.flush()

    subject = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.customer,
        principal_customer_id=customer.id,
        status='active',
    )
    session.add(subject)
    await session.flush()

    return {
        'application_id': app.id,
        'subject_id': subject.id,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_and_normalize_creates_artifact_resource_fact_binding(
    acl_service: ACLNormalizerService,
    session_factory,
    capturing_events: CapturingEventService,
) -> None:
    """All four rows are created; created_fact=True, created_resource=True; events emitted."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        payload = _make_payload()
        result = await acl_service.ingest_and_normalize(
            session,
            application_id=ids['application_id'],
            subject_id=ids['subject_id'],
            account_id=None,
            payload=payload,
            artifact_external_id='artifact-001',
        )
        await session.commit()

    assert result.created_fact is True
    assert result.created_resource is True
    assert result.artifact_id is not None
    assert result.resource_id is not None
    assert result.access_fact_id is not None
    assert result.binding_id is not None

    event_types = [e.event_type for e in capturing_events.emitted]
    assert event_types.count('inventory.access_artifact.ingested') == 1
    assert event_types.count('inventory.resource.created') == 1
    assert event_types.count('inventory.access_fact.created') == 0  # Step 12: events moved to SyncApplyService
    assert event_types.count('inventory.artifact_binding.created') == 1


@pytest.mark.asyncio
async def test_second_ingest_on_same_resource_reuses_resource(
    acl_service: ACLNormalizerService,
    session_factory,
    capturing_events: CapturingEventService,
) -> None:
    """Two different verbs on the same resource_external_id → 1 resource, 2 facts."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        payload_read = _make_payload(verb='read')
        payload_write = _make_payload(verb='write')
        result1 = await acl_service.ingest_and_normalize(
            session,
            application_id=ids['application_id'],
            subject_id=ids['subject_id'],
            account_id=None,
            payload=payload_read,
            artifact_external_id='artifact-read-001',
        )
        result2 = await acl_service.ingest_and_normalize(
            session,
            application_id=ids['application_id'],
            subject_id=ids['subject_id'],
            account_id=None,
            payload=payload_write,
            artifact_external_id='artifact-write-001',
        )
        await session.commit()

    assert result1.resource_id == result2.resource_id
    assert result2.created_resource is False
    assert result1.access_fact_id != result2.access_fact_id
    assert result2.created_fact is True

    event_types = [e.event_type for e in capturing_events.emitted]
    assert event_types.count('inventory.resource.created') == 1
    assert event_types.count('inventory.access_fact.created') == 0  # Step 12: events moved to SyncApplyService


@pytest.mark.asyncio
async def test_replay_does_not_duplicate_access_fact(
    acl_service: ACLNormalizerService,
    session_factory,
    capturing_events: CapturingEventService,
) -> None:
    """Same payload twice → 2 artifacts, 2 bindings, 1 fact; second call has created_fact=False."""
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        payload = _make_payload()
        result1 = await acl_service.ingest_and_normalize(
            session,
            application_id=ids['application_id'],
            subject_id=ids['subject_id'],
            account_id=None,
            payload=payload,
            artifact_external_id='artifact-pass-1',
        )
        await session.commit()

    async with session_factory() as session:
        result2 = await acl_service.ingest_and_normalize(
            session,
            application_id=ids['application_id'],
            subject_id=ids['subject_id'],
            account_id=None,
            payload=payload,
            artifact_external_id='artifact-pass-2',
        )
        await session.commit()

    assert result2.created_fact is False
    assert result2.access_fact_id == result1.access_fact_id
    assert result2.artifact_id != result1.artifact_id

    event_types = [e.event_type for e in capturing_events.emitted]
    assert event_types.count('inventory.access_fact.created') == 0  # Step 12: events moved to SyncApplyService
    assert event_types.count('inventory.access_artifact.ingested') == 2
    assert event_types.count('inventory.artifact_binding.created') == 2


@pytest.mark.asyncio
async def test_unknown_application_id_surfaces_error(
    acl_service: ACLNormalizerService,
    session_factory,
) -> None:
    """AccessArtifactApplicationNotFoundError propagates; no partial writes."""
    fake_app_id = uuid.uuid4()

    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        await session.commit()

    with pytest.raises(AccessArtifactApplicationNotFoundError):
        async with session_factory() as session:
            await acl_service.ingest_and_normalize(
                session,
                application_id=fake_app_id,
                subject_id=ids['subject_id'],
                account_id=None,
                payload=_make_payload(),
                artifact_external_id='artifact-bad-app',
            )


@pytest.mark.asyncio
async def test_savepoint_protects_artifact_and_resource_on_duplicate_fact(
    acl_service: ACLNormalizerService,
    session_factory,
) -> None:
    """Regression: second ingest with duplicate fact key must not roll back artifact/resource.

    If the orchestrator omits session.begin_nested(), the AccessFactService.create_fact
    internal rollback would wipe artifact and resource, and the subsequent
    binding step would fail on a dangling FK.
    """
    async with session_factory() as session:
        ids = await _make_prerequisites(session)
        payload = _make_payload()

        # First call — creates all four rows.
        result1 = await acl_service.ingest_and_normalize(
            session,
            application_id=ids['application_id'],
            subject_id=ids['subject_id'],
            account_id=None,
            payload=payload,
            artifact_external_id='savepoint-artifact-1',
        )

        # Second call — same natural key, fresh artifact_external_id.
        result2 = await acl_service.ingest_and_normalize(
            session,
            application_id=ids['application_id'],
            subject_id=ids['subject_id'],
            account_id=None,
            payload=payload,
            artifact_external_id='savepoint-artifact-2',
        )

        # All assertions from the SAME live session (no commit between calls).
        assert result2.created_fact is False

        # Artifact written in pass-2 must still be visible (raw SQL — ORM deleted Phase 15 Step 16).
        import sqlalchemy as _sa

        artifact2_row = (
            await session.execute(
                _sa.text('SELECT id FROM access_artifacts WHERE id = :id'),
                {'id': result2.artifact_id},
            )
        ).one_or_none()
        assert artifact2_row is not None, 'pass-2 artifact was rolled back — SAVEPOINT missing'

        # Resource must survive.
        resource2 = await session.get(Resource, result2.resource_id)
        assert resource2 is not None, 'resource was rolled back — SAVEPOINT missing'

        # Binding must have been written after duplicate was handled.
        binding2 = await session.get(ArtifactBinding, result2.binding_id)
        assert binding2 is not None, 'binding was not written — step after SAVEPOINT failed'

        # Refetch must return the original fact.
        assert result2.access_fact_id == result1.access_fact_id

        await session.commit()
