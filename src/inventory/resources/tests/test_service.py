# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ResourceService."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.resources.schemas import ResourcePatch
from src.inventory.resources.service import (
    DuplicateResourceAttributeError,
    DuplicateResourceError,
    ResourceApplicationNotFoundError,
    ResourceAttributeNotFoundError,
    ResourceNotFoundError,
    ResourceParentNotFoundError,
    ResourceService,
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
def service(log_service: LogService) -> ResourceService:
    return ResourceService(log_service=log_service)


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
async def test_create_resource_happy_path(
    service: ResourceService,
    session_factory,
    log_path: Path,
) -> None:
    """create_resource creates resource and emits resource.created."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-res-001',
            application_id=app_id,
            kind='database',
        )
        await session.commit()

    assert resource.id is not None
    assert resource.external_id == 'svc-res-001'
    assert resource.kind == 'database'

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'resource.created']
    assert len(created) >= 1
    assert created[-1]['component'] == 'inventory.resources'
    assert 'resource_id' in created[-1]['payload']
    assert created[-1]['payload']['kind'] == 'database'


@pytest.mark.asyncio
async def test_create_resource_bad_application_id(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource raises ResourceApplicationNotFoundError for unknown application_id."""
    with pytest.raises(ResourceApplicationNotFoundError):
        async with session_factory() as session:
            await service.create_resource(
                session,
                external_id='svc-res-bad-app',
                application_id=uuid.uuid4(),
                kind='table',
            )


@pytest.mark.asyncio
async def test_create_resource_bad_parent_id(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource raises ResourceParentNotFoundError for unknown parent_id."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await session.commit()

    with pytest.raises(ResourceParentNotFoundError):
        async with session_factory() as session:
            await service.create_resource(
                session,
                external_id='svc-res-bad-parent',
                application_id=app_id,
                kind='file',
                parent_id=uuid.uuid4(),
            )


@pytest.mark.asyncio
async def test_create_resource_duplicate(
    service: ResourceService,
    session_factory,
) -> None:
    """create_resource raises DuplicateResourceError for same (application_id, external_id)."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await service.create_resource(
            session,
            external_id='svc-dup-001',
            application_id=app_id,
            kind='table',
        )
        await session.commit()

    with pytest.raises(DuplicateResourceError):
        async with session_factory() as session:
            await service.create_resource(
                session,
                external_id='svc-dup-001',
                application_id=app_id,
                kind='view',
            )
            await session.commit()


@pytest.mark.asyncio
async def test_get_resource_found(
    service: ResourceService,
    session_factory,
    log_path: Path,
) -> None:
    """get_resource returns resource and emits resource.retrieved."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-get-001',
            application_id=app_id,
            kind='bucket',
        )
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        found = await service.get_resource(session, resource_id)

    assert found is not None
    assert found.id == resource_id
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    retrieved = [r for r in records if r.get('event_type') == 'resource.retrieved']
    assert len(retrieved) >= 1
    assert retrieved[-1]['component'] == 'inventory.resources'


@pytest.mark.asyncio
async def test_get_resource_missing(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource returns None for unknown id."""
    async with session_factory() as session:
        result = await service.get_resource(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_update_resource_privilege_level(
    service: ResourceService,
    session_factory,
    log_path: Path,
) -> None:
    """update_resource changes privilege_level and emits resource.updated."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-upd-001',
            application_id=app_id,
            kind='api',
        )
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        patch = ResourcePatch(privilege_level='admin')
        updated = await service.update_resource(session, resource_id, patch)
        await session.commit()

    assert updated.privilege_level.value == 'admin'
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    updated_events = [r for r in records if r.get('event_type') == 'resource.updated']
    assert len(updated_events) >= 1
    assert 'privilege_level' in updated_events[-1]['payload']['changed_fields']


@pytest.mark.asyncio
async def test_update_resource_no_op(
    service: ResourceService,
    session_factory,
    log_path: Path,
) -> None:
    """update_resource with same value does not emit resource.updated."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-noop-001',
            application_id=app_id,
            kind='table',
        )
        await session.commit()
        resource_id = resource.id

    count_before = len(log_path.read_text().strip().split('\n')) if log_path.exists() else 0

    async with session_factory() as session:
        patch = ResourcePatch(kind='table')
        await service.update_resource(session, resource_id, patch)
        await session.commit()

    count_after = len(log_path.read_text().strip().split('\n')) if log_path.exists() else 0
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines if line.strip()]
    updated_events = [r for r in records if r.get('event_type') == 'resource.updated']
    assert len(updated_events) == 0
    assert count_after == count_before


@pytest.mark.asyncio
async def test_add_attribute_happy_path(
    service: ResourceService,
    session_factory,
    log_path: Path,
) -> None:
    """add_attribute adds attribute and emits resource.attribute.added."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-add-001',
            application_id=app_id,
            kind='storage',
        )
        await session.flush()
        attr = await service.add_attribute(session, resource.id, 'owner', 'alice')
        await session.commit()

    assert attr.key == 'owner'
    assert attr.value == 'alice'
    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    added = [r for r in records if r.get('event_type') == 'resource.attribute.added']
    assert len(added) >= 1
    assert added[-1]['payload']['key'] == 'owner'


@pytest.mark.asyncio
async def test_add_attribute_duplicate(
    service: ResourceService,
    session_factory,
) -> None:
    """add_attribute raises DuplicateResourceAttributeError on duplicate key."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-dup-001',
            application_id=app_id,
            kind='file',
        )
        await session.flush()
        await service.add_attribute(session, resource.id, 'tag', 'v1')
        await session.commit()
        resource_id = resource.id

    with pytest.raises(DuplicateResourceAttributeError):
        async with session_factory() as session:
            from src.inventory.resources.repository import get_resource_by_id

            res = await get_resource_by_id(session, resource_id)
            assert res is not None
            await service.add_attribute(session, res.id, 'tag', 'v2')
            await session.commit()


@pytest.mark.asyncio
async def test_remove_attribute_happy_path(
    service: ResourceService,
    session_factory,
    log_path: Path,
) -> None:
    """remove_attribute removes attribute and emits resource.attribute.removed."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-rm-001',
            application_id=app_id,
            kind='queue',
        )
        await session.flush()
        await service.add_attribute(session, resource.id, 'env', 'prod')
        await session.commit()
        resource_id = resource.id

    async with session_factory() as session:
        await service.remove_attribute(session, resource_id, 'env')
        await session.commit()

    lines = log_path.read_text().strip().split('\n')
    records = [json.loads(line) for line in lines]
    removed = [r for r in records if r.get('event_type') == 'resource.attribute.removed']
    assert len(removed) >= 1
    assert removed[-1]['payload']['key'] == 'env'


@pytest.mark.asyncio
async def test_remove_attribute_missing(
    service: ResourceService,
    session_factory,
) -> None:
    """remove_attribute raises ResourceAttributeNotFoundError for missing key."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        resource = await service.create_resource(
            session,
            external_id='svc-attr-rmm-001',
            application_id=app_id,
            kind='topic',
        )
        await session.commit()
        resource_id = resource.id

    with pytest.raises(ResourceAttributeNotFoundError):
        async with session_factory() as session:
            await service.remove_attribute(session, resource_id, 'nonexistent')


@pytest.mark.asyncio
async def test_list_attributes_not_found(
    service: ResourceService,
    session_factory,
) -> None:
    """list_attributes raises ResourceNotFoundError for unknown resource."""
    with pytest.raises(ResourceNotFoundError):
        async with session_factory() as session:
            await service.list_attributes(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_get_resource_by_external_id_returns_resource_when_present(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource_by_external_id returns the resource when (application_id, external_id) exists."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        created = await service.create_resource(
            session,
            external_id='ext-lookup-001',
            application_id=app_id,
            kind='folder',
        )
        await session.commit()
        resource_id = created.id

    async with session_factory() as session:
        found = await service.get_resource_by_external_id(
            session,
            application_id=app_id,
            external_id='ext-lookup-001',
        )

    assert found is not None
    assert found.id == resource_id
    assert found.external_id == 'ext-lookup-001'


@pytest.mark.asyncio
async def test_get_resource_by_external_id_returns_none_when_absent(
    service: ResourceService,
    session_factory,
) -> None:
    """get_resource_by_external_id returns None when the (application_id, external_id) does not exist."""
    async with session_factory() as session:
        app_id = await _make_application_id(session)
        await session.commit()

    async with session_factory() as session:
        found = await service.get_resource_by_external_id(
            session,
            application_id=app_id,
            external_id='nonexistent-external-id',
        )

    assert found is None
