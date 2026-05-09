# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Resource API routes."""

from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.resources.routes import router as resources_router


@pytest.fixture
def app_with_resources(engine):
    """App with resource routes using test engine."""
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(resources_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


async def _make_application_id(engine) -> uuid.UUID:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.platform.applications.models import Application

    sf = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, autocommit=False, class_=AsyncSession)
    async with sf() as session:
        app = Application(
            name=f'test-app-{uuid.uuid4()}',
            code=f'app-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.commit()
        return app.id


@pytest.mark.asyncio
async def test_post_resource_201(app_with_resources, engine) -> None:
    """POST /resources with valid payload returns 201."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/resources',
            json={
                'external_id': 'route-res-001',
                'application_id': str(app_id),
                'kind': 'database',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['external_id'] == 'route-res-001'
    assert data['kind'] == 'database'
    assert 'id' in data
    assert 'created_at' in data
    assert 'updated_at' in data


@pytest.mark.asyncio
async def test_post_resource_422_bad_application(app_with_resources) -> None:
    """POST /resources with non-existent application_id returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/resources',
            json={
                'external_id': 'route-res-bad-app',
                'application_id': str(uuid.uuid4()),
                'kind': 'bucket',
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_resource_422_bad_parent(app_with_resources, engine) -> None:
    """POST /resources with non-existent parent_id returns 422."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/resources',
            json={
                'external_id': 'route-res-bad-parent',
                'application_id': str(app_id),
                'kind': 'file',
                'parent_id': str(uuid.uuid4()),
            },
        )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_post_resource_409_duplicate(app_with_resources, engine) -> None:
    """POST /resources with duplicate (application_id, external_id) returns 409."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        payload = {'external_id': 'dup-res-001', 'application_id': str(app_id), 'kind': 'table'}
        await client.post('/api/v0/resources', json=payload)
        response = await client.post('/api/v0/resources', json=payload)
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_get_resources_200_empty(app_with_resources) -> None:
    """GET /resources returns 200 with empty list when no resources."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/resources')
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_get_resources_200_with_filters(app_with_resources, engine) -> None:
    """GET /resources with kind filter returns matching resources only."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/resources',
            json={'external_id': 'filter-001', 'application_id': str(app_id), 'kind': 'table'},
        )
        await client.post(
            '/api/v0/resources',
            json={'external_id': 'filter-002', 'application_id': str(app_id), 'kind': 'view'},
        )
        response = await client.get('/api/v0/resources', params={'kind': 'table'})

    assert response.status_code == 200
    data = response.json()
    assert all(r['kind'] == 'table' for r in data)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_get_resource_200(app_with_resources, engine) -> None:
    """GET /resources/{id} returns 200 with resource data."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={'external_id': 'get-001', 'application_id': str(app_id), 'kind': 'api'},
        )
        resource_id = create_resp.json()['id']
        response = await client.get(f'/api/v0/resources/{resource_id}')

    assert response.status_code == 200
    assert response.json()['id'] == resource_id


@pytest.mark.asyncio
async def test_get_resource_404(app_with_resources) -> None:
    """GET /resources/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/resources/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_resource_200(app_with_resources, engine) -> None:
    """PATCH /resources/{id} updates privilege_level and returns 200."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={'external_id': 'patch-001', 'application_id': str(app_id), 'kind': 'storage'},
        )
        resource_id = create_resp.json()['id']
        response = await client.patch(
            f'/api/v0/resources/{resource_id}',
            json={'privilege_level': 'read'},
        )

    assert response.status_code == 200
    assert response.json()['privilege_level'] == 'read'


@pytest.mark.asyncio
async def test_patch_resource_404(app_with_resources) -> None:
    """PATCH /resources/{id} returns 404 for unknown id."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/resources/{uuid.uuid4()}',
            json={'privilege_level': 'admin'},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_attributes_200(app_with_resources, engine) -> None:
    """GET /resources/{id}/attributes returns 200 with attributes list."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={'external_id': 'attr-list-001', 'application_id': str(app_id), 'kind': 'queue'},
        )
        resource_id = create_resp.json()['id']
        response = await client.get(f'/api/v0/resources/{resource_id}/attributes')

    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_post_attribute_201(app_with_resources, engine) -> None:
    """POST /resources/{id}/attributes creates attribute and returns 201."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={'external_id': 'attr-post-001', 'application_id': str(app_id), 'kind': 'stream'},
        )
        resource_id = create_resp.json()['id']
        response = await client.post(
            f'/api/v0/resources/{resource_id}/attributes',
            json={'key': 'region', 'value': 'us-east-1'},
        )

    assert response.status_code == 201
    data = response.json()
    assert data['key'] == 'region'
    assert data['value'] == 'us-east-1'


@pytest.mark.asyncio
async def test_post_attribute_409_duplicate(app_with_resources, engine) -> None:
    """POST /resources/{id}/attributes returns 409 for duplicate key."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={'external_id': 'attr-dup-001', 'application_id': str(app_id), 'kind': 'table'},
        )
        resource_id = create_resp.json()['id']
        await client.post(
            f'/api/v0/resources/{resource_id}/attributes',
            json={'key': 'tag', 'value': 'v1'},
        )
        response = await client.post(
            f'/api/v0/resources/{resource_id}/attributes',
            json={'key': 'tag', 'value': 'v2'},
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_delete_attribute_204(app_with_resources, engine) -> None:
    """DELETE /resources/{id}/attributes/{key} returns 204."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={'external_id': 'attr-del-001', 'application_id': str(app_id), 'kind': 'topic'},
        )
        resource_id = create_resp.json()['id']
        await client.post(
            f'/api/v0/resources/{resource_id}/attributes',
            json={'key': 'to_delete', 'value': 'x'},
        )
        response = await client.delete(f'/api/v0/resources/{resource_id}/attributes/to_delete')

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_delete_attribute_404(app_with_resources, engine) -> None:
    """DELETE /resources/{id}/attributes/{key} returns 404 for missing key."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={'external_id': 'attr-del-404', 'application_id': str(app_id), 'kind': 'bucket'},
        )
        resource_id = create_resp.json()['id']
        response = await client.delete(f'/api/v0/resources/{resource_id}/attributes/nonexistent')

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Phase 12 Step 6 — identity round-trip tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_resource_with_identity_round_trips_through_get(app_with_resources, engine) -> None:
    """POST /resources with explicit resource_type/resource_key round-trips through GET."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={
                'external_id': 'identity-rt-001',
                'application_id': str(app_id),
                'kind': 'table',
                'resource_type': 'snowflake_table',
                'resource_key': 'finance.public.orders',
            },
        )
        assert create_resp.status_code == 201
        created = create_resp.json()
        assert created['resource_type'] == 'snowflake_table'
        assert created['resource_key'] == 'finance.public.orders'

        get_resp = await client.get(f'/api/v0/resources/{created["id"]}')
        assert get_resp.status_code == 200
        fetched = get_resp.json()
        assert fetched['resource_type'] == 'snowflake_table'
        assert fetched['resource_key'] == 'finance.public.orders'


@pytest.mark.asyncio
async def test_post_resource_without_identity_defaults_correctly(app_with_resources, engine) -> None:
    """POST /resources without identity fields defaults resource_type=kind, resource_key=external_id."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/resources',
            json={
                'external_id': 'identity-default-rt-001',
                'application_id': str(app_id),
                'kind': 'bucket',
            },
        )
    assert create_resp.status_code == 201
    data = create_resp.json()
    assert data['resource_type'] == 'bucket'
    assert data['resource_key'] == 'identity-default-rt-001'


@pytest.mark.asyncio
async def test_post_resource_identity_duplicate_returns_409(app_with_resources, engine) -> None:
    """POST /resources with same identity triple but different external_id returns 409."""
    app_id = await _make_application_id(engine)
    async with AsyncClient(
        transport=ASGITransport(app=app_with_resources),
        base_url='http://testserver',
    ) as client:
        first = await client.post(
            '/api/v0/resources',
            json={
                'external_id': 'id-dup-first',
                'application_id': str(app_id),
                'kind': 'table',
                'resource_type': 'pg_table',
                'resource_key': 'public.events',
            },
        )
        assert first.status_code == 201

        second = await client.post(
            '/api/v0/resources',
            json={
                'external_id': 'id-dup-second',
                'application_id': str(app_id),
                'kind': 'table',
                'resource_type': 'pg_table',
                'resource_key': 'public.events',
            },
        )
        assert second.status_code == 409
