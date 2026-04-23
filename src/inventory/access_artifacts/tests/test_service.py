# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessArtifactService."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.access_artifacts.service import (
    AccessArtifactApplicationNotFoundError,
    AccessArtifactNotFoundError,
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
async def test_upsert_artifact_fresh_emits_ingested_event(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_artifact on a fresh key returns (artifact, True) and emits inventory.access_artifact.ingested."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, was_inserted = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-admin',
            payload={'name': 'ADMIN'},
            ingest_batch_id='batch-001',
        )
        await session.commit()

    assert was_inserted is True
    assert artifact.id is not None
    assert artifact.artifact_type == 'sap_role'
    assert artifact.external_id == 'role-admin'
    assert artifact.payload == {'name': 'ADMIN'}

    emitted = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.access_artifacts'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(artifact.id)
    assert envelope.payload['artifact_id'] == str(artifact.id)
    assert envelope.payload['application_id'] == str(app_id)
    assert envelope.payload['artifact_type'] == 'sap_role'
    assert 'source_kind' not in envelope.payload
    assert envelope.payload['external_id'] == 'role-admin'
    assert envelope.payload['ingest_batch_id'] == 'batch-001'
    assert 'raw_name' in envelope.payload
    assert 'effect' in envelope.payload
    assert 'valid_from' in envelope.payload
    assert 'valid_until' in envelope.payload


@pytest.mark.asyncio
async def test_upsert_artifact_update_path_emits_no_event(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Second upsert on the same triple returns (artifact, False) and emits no additional event."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        _, was_inserted_1 = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-update-test',
            payload={'v': 1},
        )
        await session.commit()

    assert was_inserted_1 is True

    async with session_factory() as session:
        _, was_inserted_2 = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-update-test',
            payload={'v': 2},
        )
        await session.commit()

    assert was_inserted_2 is False

    # Only one .ingested event across both calls.
    emitted = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_upsert_artifact_update_path_refreshes_payload(
    service: AccessArtifactService,
    session_factory,
) -> None:
    """Second upsert refreshes payload and returns the same artifact id."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact1, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-refresh',
            payload={'privilege': 'SELECT'},
            ingest_batch_id='batch-a',
        )
        await session.commit()
        original_id = artifact1.id

    async with session_factory() as session:
        artifact2, was_inserted = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-refresh',
            payload={'privilege': 'INSERT'},
            ingest_batch_id='batch-b',
        )
        await session.commit()

    assert was_inserted is False
    assert artifact2.id == original_id
    assert artifact2.payload == {'privilege': 'INSERT'}
    assert artifact2.ingest_batch_id == 'batch-b'


@pytest.mark.asyncio
async def test_upsert_artifact_observed_at_defaults_to_now(
    service: AccessArtifactService,
    session_factory,
) -> None:
    """upsert_artifact without observed_at defaults to approximately now."""
    before = datetime.now(UTC)
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-001',
            payload={},
        )
        await session.commit()
    after = datetime.now(UTC)

    oa = artifact.observed_at
    if oa.tzinfo is None:
        oa = oa.replace(tzinfo=UTC)
    assert before <= oa <= after


@pytest.mark.asyncio
async def test_upsert_artifact_explicit_observed_at_persisted(
    service: AccessArtifactService,
    session_factory,
) -> None:
    """upsert_artifact with explicit observed_at persists it verbatim."""
    explicit_ts = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-explicit',
            payload={},
            observed_at=explicit_ts,
        )
        await session.commit()

    oa = artifact.observed_at
    if oa.tzinfo is None:
        oa = oa.replace(tzinfo=UTC)
    assert oa == explicit_ts


@pytest.mark.asyncio
async def test_upsert_artifact_bad_application_id(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_artifact raises AccessArtifactApplicationNotFoundError for unknown application."""
    with pytest.raises(AccessArtifactApplicationNotFoundError):
        async with session_factory() as session:
            await service.upsert_artifact(
                session,
                application_id=uuid.uuid4(),
                artifact_type='acl_entry',
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
    """get_artifact returns artifact without emitting any event."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-select',
            payload={'privilege': 'SELECT'},
        )
        await session.commit()
        artifact_id = artifact.id

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
async def test_upsert_artifact_propagates_correlation_id(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_artifact propagates an explicit correlation_id into the envelope."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-admin-corr',
            payload={'name': 'ADMIN'},
            correlation_id='trace-artifact-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'trace-artifact-xyz'


@pytest.mark.asyncio
async def test_upsert_artifact_forwards_permitted_fields_to_repo(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_artifact forwards all four permitted universal fields to the repository."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, was_inserted = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-permitted-svc',
            payload={'name': 'ADMIN'},
            raw_name='SAP ADMIN Role',
            effect='grant',
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_until=datetime(2026, 12, 31, tzinfo=UTC),
        )
        await session.commit()

    assert was_inserted is True
    assert artifact.raw_name == 'SAP ADMIN Role'
    assert artifact.effect == 'grant'
    assert artifact.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert artifact.valid_until == datetime(2026, 12, 31, tzinfo=UTC)

    emitted = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(emitted) == 1


@pytest.mark.asyncio
async def test_upsert_artifact_ingested_event_payload_includes_permitted_fields(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Step 10 inversion of Q7 guard: the emitted .ingested event payload includes
    all four permitted universal fields with correct values, and timestamps are
    serialized as ISO-8601 strings with timezone suffix.
    """
    vf = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    vu = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-q7-step10',
            payload={'name': 'ADMIN'},
            raw_name='SAP ADMIN Role',
            effect='grant',
            valid_from=vf,
            valid_until=vu,
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(emitted) == 1
    event_payload = emitted[0].payload

    # Exactly nine keys — the Step 8 shape plus the four permitted fields.
    assert set(event_payload.keys()) == {
        'artifact_id',
        'application_id',
        'artifact_type',
        'external_id',
        'ingest_batch_id',
        'raw_name',
        'effect',
        'valid_from',
        'valid_until',
    }
    assert event_payload['raw_name'] == 'SAP ADMIN Role'
    assert event_payload['effect'] == 'grant'
    # Timestamps must be ISO-8601 strings including timezone suffix.
    assert event_payload['valid_from'] == '2026-01-01T00:00:00+00:00'
    assert event_payload['valid_until'] == '2026-12-31T23:59:59+00:00'


@pytest.mark.asyncio
async def test_upsert_artifact_ingested_event_null_permitted_fields(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """When upsert_artifact is called without permitted fields, the .ingested event
    payload carries all four keys with None values — keys are present, not omitted.
    """
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-null-permitted',
            payload={'name': 'VIEWER'},
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(emitted) == 1
    event_payload = emitted[0].payload

    assert 'raw_name' in event_payload
    assert 'effect' in event_payload
    assert 'valid_from' in event_payload
    assert 'valid_until' in event_payload
    assert event_payload['raw_name'] is None
    assert event_payload['effect'] is None
    assert event_payload['valid_from'] is None
    assert event_payload['valid_until'] is None


@pytest.mark.asyncio
async def test_upsert_artifact_update_path_refreshes_permitted_fields(
    service: AccessArtifactService,
    session_factory,
) -> None:
    """Second upsert via service with new permitted field values refreshes them and emits no event."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact1, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-permitted-update',
            payload={'v': 1},
            raw_name='Old Name',
            effect='allow',
        )
        await session.commit()
        original_id = artifact1.id

    async with session_factory() as session:
        artifact2, was_inserted = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-permitted-update',
            payload={'v': 2},
            raw_name='New Name',
            effect='deny',
        )
        await session.commit()

    assert was_inserted is False
    assert artifact2.id == original_id
    assert artifact2.raw_name == 'New Name'
    assert artifact2.effect == 'deny'


@pytest.mark.asyncio
async def test_upsert_artifact_generates_correlation_id_when_omitted(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_artifact generates a uuid4 hex correlation_id when caller omits it."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-admin-nocorr',
            payload={'name': 'ADMIN'},
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(emitted) == 1
    cid = emitted[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32  # uuid4().hex = 32 hex chars
    assert cid.isalnum()


# ---------------------------------------------------------------------------
# Step 11 — Tombstone lifecycle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_artifact_transitions_active_to_inactive(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """tombstone_artifact on an active row flips is_active=False, stamps tombstoned_at,
    returns (artifact, True), and emits exactly one inventory.access_artifact.tombstoned
    event with a six-key payload and ISO-8601 timestamps.
    """
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-tombstone-happy',
            payload={'name': 'ADMIN'},
        )
        await session.commit()
        artifact_id = artifact.id

    capturing_events.clear()
    observed = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        result, was_tombstoned = await service.tombstone_artifact(
            session,
            artifact_id=artifact_id,
            observed_at=observed,
            correlation_id='trace-tombstone-001',
        )
        await session.commit()

    assert was_tombstoned is True
    assert result.is_active is False
    assert result.tombstoned_at is not None
    # tombstoned_at should equal observed_at
    ts = result.tombstoned_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    assert ts == observed

    emitted = capturing_events.filter_by_type('inventory.access_artifact.tombstoned')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.correlation_id == 'trace-tombstone-001'
    p = envelope.payload
    assert set(p.keys()) == {
        'artifact_id',
        'application_id',
        'artifact_type',
        'external_id',
        'tombstoned_at',
        'observed_at',
    }
    assert p['artifact_id'] == str(artifact_id)
    assert p['application_id'] == str(app_id)
    assert p['artifact_type'] == 'sap_role'
    assert p['external_id'] == 'role-tombstone-happy'
    # Both timestamps must be ISO-8601 strings
    assert isinstance(p['tombstoned_at'], str)
    assert isinstance(p['observed_at'], str)
    assert p['tombstoned_at'] == observed.isoformat()
    assert p['observed_at'] == observed.isoformat()


@pytest.mark.asyncio
async def test_tombstone_artifact_idempotent_on_already_tombstoned(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """Calling tombstone_artifact on an already-tombstoned row returns (artifact, False)
    and emits nothing. tombstoned_at is unchanged from the first tombstone call.
    """
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-tombstone-idempotent',
            payload={'name': 'VIEWER'},
        )
        await session.commit()
        artifact_id = artifact.id

    first_observed = datetime(2026, 4, 23, 9, 0, 0, tzinfo=UTC)
    async with session_factory() as session:
        await service.tombstone_artifact(
            session,
            artifact_id=artifact_id,
            observed_at=first_observed,
        )
        await session.commit()

    capturing_events.clear()
    second_observed = datetime(2026, 4, 23, 11, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        result, was_tombstoned = await service.tombstone_artifact(
            session,
            artifact_id=artifact_id,
            observed_at=second_observed,
        )
        await session.commit()

    assert was_tombstoned is False
    assert result.is_active is False
    # tombstoned_at must still reflect the FIRST tombstone, not the second call
    ts = result.tombstoned_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    assert ts == first_observed
    # No event emitted on idempotent call
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_tombstone_artifact_not_found_raises(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """tombstone_artifact with unknown artifact_id raises AccessArtifactNotFoundError
    and emits no event.
    """
    with pytest.raises(AccessArtifactNotFoundError):
        async with session_factory() as session:
            await service.tombstone_artifact(
                session,
                artifact_id=uuid.uuid4(),
            )

    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_tombstone_artifact_default_observed_at(
    service: AccessArtifactService,
    session_factory,
) -> None:
    """tombstone_artifact without observed_at defaults to approximately datetime.now(UTC)."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-tombstone-default-ts',
            payload={},
        )
        await session.commit()
        artifact_id = artifact.id

    before = datetime.now(UTC)
    async with session_factory() as session:
        result, was_tombstoned = await service.tombstone_artifact(
            session,
            artifact_id=artifact_id,
        )
        await session.commit()
    after = datetime.now(UTC)

    assert was_tombstoned is True
    ts = result.tombstoned_at
    assert ts is not None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    assert before <= ts <= after


@pytest.mark.asyncio
async def test_tombstone_artifact_emits_correlation_id(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """tombstone_artifact propagates explicit correlation_id; when omitted generates a fresh uuid4 hex."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-tombstone-corr-explicit',
            payload={},
        )
        await session.commit()
        artifact_id_explicit = artifact.id

    async with session_factory() as session:
        await service.tombstone_artifact(
            session,
            artifact_id=artifact_id_explicit,
            correlation_id='my-correlation-id-xyz',
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.access_artifact.tombstoned')
    assert len(emitted) == 1
    assert emitted[0].correlation_id == 'my-correlation-id-xyz'
    capturing_events.clear()

    # Omit correlation_id → service generates fresh uuid4 hex
    async with session_factory() as session:
        app_id2 = await _make_application_id(session)
        artifact2, _ = await service.upsert_artifact(
            session,
            application_id=app_id2,
            artifact_type='acl_entry',
            external_id='acl-tombstone-corr-auto',
            payload={},
        )
        await session.commit()
        artifact_id_auto = artifact2.id

    async with session_factory() as session:
        await service.tombstone_artifact(
            session,
            artifact_id=artifact_id_auto,
        )
        await session.commit()

    emitted2 = capturing_events.filter_by_type('inventory.access_artifact.tombstoned')
    assert len(emitted2) == 1
    cid = emitted2[0].correlation_id
    assert isinstance(cid, str)
    assert len(cid) == 32
    assert cid.isalnum()


@pytest.mark.asyncio
async def test_upsert_artifact_reactivates_tombstoned_row(
    service: AccessArtifactService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """upsert_artifact on a tombstoned row reactivates it: is_active=True, tombstoned_at=None.
    was_inserted=False (still an UPDATE — same row, not a fresh insert).

    Observability gap (Q3): no inventory.access_artifact.reactivated event is emitted.
    The .ingested event does NOT fire either (update path). This is a known, intentional
    omission deferred to a future step — see TASK.md Q3 for the rationale.
    """
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-reactivation-test',
            payload={'v': 1},
        )
        await session.commit()
        artifact_id = artifact.id

    # Tombstone the row
    async with session_factory() as session:
        await service.tombstone_artifact(session, artifact_id=artifact_id)
        await session.commit()

    capturing_events.clear()

    # Re-upsert with the same identity triple → should reactivate
    async with session_factory() as session:
        revived, was_inserted = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-reactivation-test',
            payload={'v': 2},
        )
        await session.commit()

    assert was_inserted is False  # UPDATE, not INSERT
    assert revived.id == artifact_id  # same row
    assert revived.is_active is True
    assert revived.tombstoned_at is None

    # No .ingested event on the update path; no .reactivated event (Q3 — deferred)
    assert capturing_events.emitted == []


@pytest.mark.asyncio
async def test_list_artifacts_is_active_filter(
    service: AccessArtifactService,
    session_factory,
) -> None:
    """list_artifacts(is_active=True/False/None) filters correctly."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        active_artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-active-filter',
            payload={},
        )
        inactive_artifact, _ = await service.upsert_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-inactive-filter',
            payload={},
        )
        await session.commit()
        active_id = active_artifact.id
        inactive_id = inactive_artifact.id

    # Tombstone the inactive one
    async with session_factory() as session:
        await service.tombstone_artifact(session, artifact_id=inactive_id)
        await session.commit()

    # is_active=True → only active
    async with session_factory() as session:
        results = await service.list_artifacts(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            is_active=True,
        )
    result_ids = {r.id for r in results}
    assert active_id in result_ids
    assert inactive_id not in result_ids

    # is_active=False → only tombstoned
    async with session_factory() as session:
        results = await service.list_artifacts(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            is_active=False,
        )
    result_ids = {r.id for r in results}
    assert inactive_id in result_ids
    assert active_id not in result_ids

    # is_active=None → both
    async with session_factory() as session:
        results = await service.list_artifacts(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            is_active=None,
        )
    result_ids = {r.id for r in results}
    assert active_id in result_ids
    assert inactive_id in result_ids
