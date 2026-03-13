# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessArtifactService."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.access_artifacts.service import (
    AccessArtifactApplicationNotFoundError,
    AccessArtifactService,
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
def service(log_service: LogService) -> AccessArtifactService:
    return AccessArtifactService(log_service=log_service)


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
async def test_create_artifact_happy_path(
    service: AccessArtifactService,
    session_factory,
    log_path: Path,
) -> None:
    """create_artifact creates artifact and emits access_artifact.created."""
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

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'access_artifact.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'inventory.access_artifacts'
    assert 'artifact_id' in created[-1]['payload']
    assert created[-1]['payload']['application_id'] == str(app_id)
    assert created[-1]['payload']['source_kind'] == 'sap_role'
    assert created[-1]['payload']['external_id'] == 'role-admin'


@pytest.mark.asyncio
async def test_create_artifact_bad_application_id(
    service: AccessArtifactService,
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


@pytest.mark.asyncio
async def test_get_artifact_found(
    service: AccessArtifactService,
    session_factory,
    log_path: Path,
) -> None:
    """get_artifact returns artifact and emits access_artifact.retrieved."""
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

    async with session_factory() as session:
        found = await service.get_artifact(session, artifact_id)

    assert found is not None
    assert found.id == artifact_id

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'access_artifact.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'inventory.access_artifacts'


@pytest.mark.asyncio
async def test_get_artifact_missing(
    service: AccessArtifactService,
    session_factory,
    log_path: Path,
) -> None:
    """get_artifact returns None for unknown id, no event emitted."""
    async with session_factory() as session:
        result = await service.get_artifact(session, uuid.uuid4())

    assert result is None
    assert not log_path.exists()
