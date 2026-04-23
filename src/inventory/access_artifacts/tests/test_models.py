# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for AccessArtifact model constraints and indexes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.access_artifacts.models import AccessArtifact


async def _make_application_id(session) -> uuid.UUID:
    """Create an Application and return its id."""
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
async def test_access_artifact_columns_exist() -> None:
    """AccessArtifact table has artifact_type, observed_at, is_active, tombstoned_at columns."""
    cols = {c.name for c in AccessArtifact.__table__.columns}
    assert 'artifact_type' in cols
    assert 'source_kind' not in cols
    assert 'observed_at' in cols
    assert 'is_active' in cols
    assert 'tombstoned_at' in cols
    assert 'raw_name' in cols
    assert 'effect' in cols
    assert 'valid_from' in cols
    assert 'valid_until' in cols


@pytest.mark.asyncio
async def test_access_artifact_column_nullability() -> None:
    """observed_at and is_active are NOT NULL; tombstoned_at and permitted universal fields are nullable."""
    cols = {c.name: c for c in AccessArtifact.__table__.columns}
    assert cols['observed_at'].nullable is False
    assert cols['is_active'].nullable is False
    assert cols['tombstoned_at'].nullable is True
    assert cols['raw_name'].nullable is True
    assert cols['effect'].nullable is True
    assert cols['valid_from'].nullable is True
    assert cols['valid_until'].nullable is True


@pytest.mark.asyncio
async def test_access_artifact_unique_constraint_in_table_args() -> None:
    """UNIQUE uq_access_artifacts_application_id_artifact_type_external_id present in __table_args__."""
    constraint_names = {c.name for c in AccessArtifact.__table__.constraints}
    assert 'uq_access_artifacts_application_id_artifact_type_external_id' in constraint_names


@pytest.mark.asyncio
async def test_access_artifact_creation_stores_all_fields(session_factory) -> None:
    """Happy path: artifact with all fields persists and ingested_at is auto-set."""
    from datetime import UTC, datetime

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        now = datetime.now(UTC)
        artifact = AccessArtifact(
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-admin-001',
            payload={'name': 'ADMIN', 'description': 'Full admin role'},
            ingest_batch_id='batch-2026-001',
            observed_at=now,
        )
        session.add(artifact)
        await session.flush()
        await session.refresh(artifact)

        assert artifact.id is not None
        assert artifact.application_id == app_id
        assert artifact.artifact_type == 'sap_role'
        assert artifact.external_id == 'role-admin-001'
        assert artifact.payload == {'name': 'ADMIN', 'description': 'Full admin role'}
        assert artifact.ingest_batch_id == 'batch-2026-001'
        assert artifact.ingested_at is not None
        assert artifact.observed_at is not None
        assert artifact.is_active is True
        assert artifact.tombstoned_at is None


@pytest.mark.asyncio
async def test_access_artifact_fk_to_application(session_factory) -> None:
    """Artifact with non-existent application_id raises IntegrityError."""
    from datetime import UTC, datetime

    async with session_factory() as session:
        artifact = AccessArtifact(
            application_id=uuid.uuid4(),
            artifact_type='acl_entry',
            external_id='acl-001',
            payload={'permission': 'read'},
            observed_at=datetime.now(UTC),
        )
        session.add(artifact)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_access_artifact_duplicate_identity_raises_integrity_error(session_factory) -> None:
    """Two artifacts with same (application_id, artifact_type, external_id) raise IntegrityError."""
    from datetime import UTC, datetime

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        now = datetime.now(UTC)
        a1 = AccessArtifact(
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-001',
            payload={'privilege': 'SELECT'},
            observed_at=now,
        )
        session.add(a1)
        await session.flush()

        a2 = AccessArtifact(
            application_id=app_id,
            artifact_type='db_grant',
            external_id='grant-001',
            payload={'privilege': 'INSERT'},
            observed_at=now,
        )
        session.add(a2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_access_artifact_permitted_fields_round_trip_all_set(session_factory) -> None:
    """ORM round-trip: artifact with all four permitted universal fields persisted and returned."""
    from datetime import UTC, datetime

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        valid_from = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        valid_until = datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)
        artifact = AccessArtifact(
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-permitted-fields',
            payload={'name': 'ADMIN'},
            observed_at=datetime.now(UTC),
            raw_name='SAP ADMIN Role',
            effect='grant',
            valid_from=valid_from,
            valid_until=valid_until,
        )
        session.add(artifact)
        await session.flush()
        await session.refresh(artifact)

        assert artifact.raw_name == 'SAP ADMIN Role'
        assert artifact.effect == 'grant'
        assert artifact.valid_from is not None
        assert artifact.valid_until is not None


@pytest.mark.asyncio
async def test_access_artifact_permitted_fields_round_trip_all_null(session_factory) -> None:
    """ORM round-trip: artifact without permitted universal fields defaults to NULL."""
    from datetime import UTC, datetime

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact = AccessArtifact(
            application_id=app_id,
            artifact_type='acl_entry',
            external_id='acl-permitted-null',
            payload={'permission': 'read'},
            observed_at=datetime.now(UTC),
        )
        session.add(artifact)
        await session.flush()
        await session.refresh(artifact)

        assert artifact.raw_name is None
        assert artifact.effect is None
        assert artifact.valid_from is None
        assert artifact.valid_until is None
