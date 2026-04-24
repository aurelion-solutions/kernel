# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for ArtifactBinding model constraints and indexes."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.artifact_bindings.models import ArtifactBinding

# ---------------------------------------------------------------------------
# Helpers
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


async def _make_access_artifact(session, application_id: uuid.UUID) -> uuid.UUID:
    from datetime import UTC, datetime

    from src.inventory.access_artifacts.models import AccessArtifact

    artifact = AccessArtifact(
        application_id=application_id,
        artifact_type='acl_entry',
        external_id=str(uuid.uuid4()),
        payload={'raw': 'data'},
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(artifact)
    await session.flush()
    return artifact.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_binding_creation_stores_polymorphic_fields(session_factory) -> None:
    """Happy path: create binding with target_type/target_id, verify all fields persisted."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_access_artifact(session, app_id)
        target_id = uuid.uuid4()

        binding = ArtifactBinding(
            artifact_id=artifact_id,
            target_type='resource',
            target_id=target_id,
        )
        session.add(binding)
        await session.flush()
        await session.refresh(binding)

        assert binding.id is not None
        assert binding.artifact_id == artifact_id
        assert binding.target_type == 'resource'
        assert binding.target_id == target_id
        assert binding.created_at is not None


@pytest.mark.asyncio
async def test_artifact_binding_fk_to_artifact(session_factory) -> None:
    """ArtifactBinding with non-existent artifact_id raises IntegrityError (FK violation)."""
    async with session_factory() as session:
        binding = ArtifactBinding(
            artifact_id=uuid.uuid4(),  # non-existent
            target_type='resource',
            target_id=uuid.uuid4(),
        )
        session.add(binding)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_artifact_binding_unique_constraint_enforced(session_factory) -> None:
    """UNIQUE (artifact_id, target_type, target_id) prevents duplicate bindings."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_access_artifact(session, app_id)
        target_id = uuid.uuid4()

        b1 = ArtifactBinding(
            artifact_id=artifact_id,
            target_type='resource',
            target_id=target_id,
        )
        session.add(b1)
        await session.flush()

        b2 = ArtifactBinding(
            artifact_id=artifact_id,
            target_type='resource',
            target_id=target_id,
        )
        session.add(b2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_artifact_binding_different_target_types_allowed(session_factory) -> None:
    """Same (artifact_id, target_id) with different target_type values is allowed."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        artifact_id = await _make_access_artifact(session, app_id)
        target_id = uuid.uuid4()

        b1 = ArtifactBinding(
            artifact_id=artifact_id,
            target_type='resource',
            target_id=target_id,
        )
        b2 = ArtifactBinding(
            artifact_id=artifact_id,
            target_type='account',
            target_id=target_id,
        )
        session.add(b1)
        session.add(b2)
        await session.flush()

        assert b1.id is not None
        assert b2.id is not None
        assert b1.id != b2.id
