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
async def test_access_artifact_creation_stores_all_fields(session_factory) -> None:
    """Happy path: artifact with all fields persists and ingested_at is auto-set."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        artifact = AccessArtifact(
            application_id=app_id,
            source_kind='sap_role',
            external_id='role-admin-001',
            payload={'name': 'ADMIN', 'description': 'Full admin role'},
            ingest_batch_id='batch-2026-001',
        )
        session.add(artifact)
        await session.flush()
        await session.refresh(artifact)

        assert artifact.id is not None
        assert artifact.application_id == app_id
        assert artifact.source_kind == 'sap_role'
        assert artifact.external_id == 'role-admin-001'
        assert artifact.payload == {'name': 'ADMIN', 'description': 'Full admin role'}
        assert artifact.ingest_batch_id == 'batch-2026-001'
        assert artifact.ingested_at is not None


@pytest.mark.asyncio
async def test_access_artifact_fk_to_application(session_factory) -> None:
    """Artifact with non-existent application_id raises IntegrityError."""
    async with session_factory() as session:
        artifact = AccessArtifact(
            application_id=uuid.uuid4(),
            source_kind='acl_entry',
            external_id='acl-001',
            payload={'permission': 'read'},
        )
        session.add(artifact)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_access_artifact_append_only_allows_duplicate_external_id(session_factory) -> None:
    """Two artifacts with same (application_id, source_kind, external_id) both persist (no uniqueness)."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        a1 = AccessArtifact(
            application_id=app_id,
            source_kind='db_grant',
            external_id='grant-001',
            payload={'privilege': 'SELECT'},
        )
        a2 = AccessArtifact(
            application_id=app_id,
            source_kind='db_grant',
            external_id='grant-001',
            payload={'privilege': 'INSERT'},
        )
        session.add(a1)
        session.add(a2)
        await session.flush()
        await session.refresh(a1)
        await session.refresh(a2)

        assert a1.id != a2.id
        assert a1.external_id == a2.external_id
