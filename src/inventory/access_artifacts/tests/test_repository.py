# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Repository tests for AccessArtifact."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.access_artifacts.repository import (
    list_access_artifacts,
    upsert_access_artifact,
)


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


@pytest.mark.asyncio
async def test_upsert_fresh_returns_was_inserted_true(session_factory) -> None:
    """upsert_access_artifact on a fresh identity triple returns was_inserted=True."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
        artifact, was_inserted = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-001',
            payload={'name': 'ADMIN'},
            ingest_batch_id='batch-001',
            observed_at=now,
        )
        await session.commit()

    assert was_inserted is True
    assert artifact.id is not None
    assert artifact.artifact_type == 'sap_role'
    assert artifact.external_id == 'role-001'
    assert artifact.payload == {'name': 'ADMIN'}
    assert artifact.ingest_batch_id == 'batch-001'
    assert artifact.observed_at.replace(tzinfo=UTC) == now
    assert artifact.is_active is True
    assert artifact.tombstoned_at is None


@pytest.mark.asyncio
async def test_upsert_existing_returns_was_inserted_false_and_refreshes_row(session_factory) -> None:
    """Second upsert on the same identity triple returns was_inserted=False and refreshes fields."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        ts1 = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
        artifact1, _ = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-001',
            payload={'privilege': 'SELECT'},
            ingest_batch_id='batch-001',
            observed_at=ts1,
        )
        await session.commit()
        original_id = artifact1.id

    ts2 = datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC)
    async with session_factory() as session:
        artifact2, was_inserted = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-001',
            payload={'privilege': 'INSERT'},
            ingest_batch_id='batch-002',
            observed_at=ts2,
        )
        await session.commit()

    assert was_inserted is False
    assert artifact2.id == original_id
    assert artifact2.payload == {'privilege': 'INSERT'}
    assert artifact2.ingest_batch_id == 'batch-002'
    assert artifact2.observed_at.replace(tzinfo=UTC) == ts2


@pytest.mark.asyncio
async def test_upsert_preserves_tombstone_state(session_factory) -> None:
    """Upsert does not reactivate a tombstoned row — is_active and tombstoned_at are preserved.

    This test locks Decision Q4 from TASK.md as a regression guard.
    Lifecycle transitions (reactivation) are Step 11's responsibility.
    """
    from src.inventory.access_artifacts.models import AccessArtifact

    tombstone_ts = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        # Manually seed a tombstoned row via ORM (no service layer to avoid coupling).
        row = AccessArtifact(
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-tombstoned',
            payload={'v': 1},
            ingest_batch_id=None,
            observed_at=datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC),
            is_active=False,
            tombstoned_at=tombstone_ts,
        )
        session.add(row)
        await session.flush()
        original_id = row.id
        await session.commit()

    ts2 = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as session:
        artifact, was_inserted = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-tombstoned',
            payload={'v': 2},
            ingest_batch_id='batch-new',
            observed_at=ts2,
        )
        await session.commit()

    # Row was updated, not inserted.
    assert was_inserted is False
    assert artifact.id == original_id

    # Payload and observed_at refreshed.
    assert artifact.payload == {'v': 2}
    assert artifact.observed_at.replace(tzinfo=UTC) == ts2

    # Lifecycle state untouched — tombstone is preserved.
    assert artifact.is_active is False
    assert artifact.tombstoned_at is not None
    assert artifact.tombstoned_at.replace(tzinfo=UTC) == tombstone_ts


@pytest.mark.asyncio
async def test_upsert_access_artifact_lifecycle_fields_round_trip(session_factory) -> None:
    """upsert_access_artifact persists artifact_type, observed_at, is_active, tombstoned_at on fresh insert."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
        artifact, was_inserted = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-001-lifecycle',
            payload={'name': 'ADMIN'},
            ingest_batch_id='batch-001',
            observed_at=now,
        )
        await session.commit()

    assert was_inserted is True
    assert artifact.artifact_type == 'sap_role'
    assert artifact.observed_at.replace(tzinfo=UTC) == now
    assert artifact.is_active is True
    assert artifact.tombstoned_at is None


@pytest.mark.asyncio
async def test_upsert_with_permitted_fields_persists_values(session_factory) -> None:
    """upsert_access_artifact with all four permitted fields set persists and returns them."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        valid_from = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        valid_until = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
        artifact, was_inserted = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-permitted-001',
            payload={'name': 'ADMIN'},
            ingest_batch_id='batch-001',
            observed_at=datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC),
            raw_name='SAP ADMIN Role',
            effect='grant',
            valid_from=valid_from,
            valid_until=valid_until,
        )
        await session.commit()

    assert was_inserted is True
    assert artifact.raw_name == 'SAP ADMIN Role'
    assert artifact.effect == 'grant'
    assert artifact.valid_from == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert artifact.valid_until == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)


@pytest.mark.asyncio
async def test_upsert_refreshes_permitted_fields_on_update(session_factory) -> None:
    """Second upsert with different permitted field values refreshes them in place."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact1, _ = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-permitted-refresh',
            payload={'name': 'ADMIN'},
            ingest_batch_id='batch-001',
            observed_at=datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC),
            raw_name='Old Name',
            effect='allow',
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_until=datetime(2026, 6, 30, tzinfo=UTC),
        )
        await session.commit()
        original_id = artifact1.id

    async with session_factory() as session:
        artifact2, was_inserted = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-permitted-refresh',
            payload={'name': 'ADMIN v2'},
            ingest_batch_id='batch-002',
            observed_at=datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC),
            raw_name='New Name',
            effect='deny',
            valid_from=datetime(2026, 7, 1, tzinfo=UTC),
            valid_until=datetime(2026, 12, 31, tzinfo=UTC),
        )
        await session.commit()

    assert was_inserted is False
    assert artifact2.id == original_id
    assert artifact2.raw_name == 'New Name'
    assert artifact2.effect == 'deny'
    assert artifact2.valid_from is not None
    assert artifact2.valid_until is not None


@pytest.mark.asyncio
async def test_upsert_resets_permitted_fields_to_null_when_omitted(session_factory) -> None:
    """Second upsert passing None for permitted fields resets them to NULL."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-permitted-reset',
            payload={'privilege': 'SELECT'},
            ingest_batch_id='batch-001',
            observed_at=datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC),
            raw_name='Some Name',
            effect='permit',
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_until=datetime(2026, 12, 31, tzinfo=UTC),
        )
        await session.commit()

    async with session_factory() as session:
        artifact, was_inserted = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-permitted-reset',
            payload={'privilege': 'SELECT'},
            ingest_batch_id='batch-002',
            observed_at=datetime(2026, 4, 24, 11, 0, 0, tzinfo=UTC),
            raw_name=None,
            effect=None,
            valid_from=None,
            valid_until=None,
        )
        await session.commit()

    assert was_inserted is False
    assert artifact.raw_name is None
    assert artifact.effect is None
    assert artifact.valid_from is None
    assert artifact.valid_until is None


@pytest.mark.asyncio
async def test_list_access_artifacts_filter_by_artifact_type(session_factory) -> None:
    """list_access_artifacts filters by artifact_type correctly."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        now = datetime.now(UTC)
        await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-001-filter',
            payload={},
            ingest_batch_id=None,
            observed_at=now,
        )
        await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-001-filter',
            payload={},
            ingest_batch_id=None,
            observed_at=now,
        )
        await session.commit()

    async with session_factory() as session:
        results = await list_access_artifacts(session, artifact_type='sap_role')

    sap_results = [r for r in results if r.external_id == 'role-001-filter']
    assert len(sap_results) == 1
    assert sap_results[0].artifact_type == 'sap_role'
