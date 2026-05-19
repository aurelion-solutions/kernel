# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Person API routes."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.persons.routes import router as persons_router


@pytest.fixture
def app_with_persons(engine):
    """App with person routes using test engine."""
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
    app.include_router(persons_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_post_persons_returns_201(app_with_persons) -> None:
    """POST /persons with valid body returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-1', 'full_name': 'Alice'},
        )
    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['external_id'] == 'ext-1'
    assert data['full_name'] == 'Alice'


@pytest.mark.asyncio
async def test_get_persons_returns_envelope(app_with_persons) -> None:
    """GET /persons?limit=10&offset=0 returns 200 with envelope."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-list', 'full_name': 'For list'},
        )
        response = await client.get('/api/v0/persons?limit=10&offset=0')
    assert response.status_code == 200
    data = response.json()
    assert 'items' in data
    assert 'total' in data
    assert 'limit' in data
    assert 'offset' in data
    assert isinstance(data['items'], list)
    assert len(data['items']) >= 1
    assert data['limit'] == 10
    assert data['offset'] == 0


@pytest.mark.asyncio
async def test_get_persons_missing_params_returns_422(app_with_persons) -> None:
    """GET /persons without params returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/persons')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_persons_missing_offset_returns_422(app_with_persons) -> None:
    """GET /persons?limit=10 without offset returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/persons?limit=10')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_persons_limit_zero_returns_422(app_with_persons) -> None:
    """GET /persons?limit=0&offset=0 returns 422 (ge=1)."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/persons?limit=0&offset=0')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_persons_limit_too_large_returns_422(app_with_persons) -> None:
    """GET /persons?limit=1001&offset=0 returns 422 (le=1000)."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/persons?limit=1001&offset=0')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_persons_past_the_end_returns_empty_items(app_with_persons) -> None:
    """GET /persons?limit=10&offset=9999 returns 200 with empty items and real total."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-pe', 'full_name': 'Past end'},
        )
        response = await client.get('/api/v0/persons?limit=10&offset=9999')
    assert response.status_code == 200
    data = response.json()
    assert data['items'] == []
    assert data['total'] >= 1
    assert data['limit'] == 10
    assert data['offset'] == 9999


@pytest.mark.asyncio
async def test_get_persons_id_returns_200(app_with_persons) -> None:
    """GET /persons/{id} returns 200 when found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-get', 'full_name': 'For get'},
        )
    assert create_resp.status_code == 201
    person_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/persons/{person_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == person_id


@pytest.mark.asyncio
async def test_get_persons_id_missing_returns_404(app_with_persons) -> None:
    """GET /persons/{id} returns 404 when not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/persons/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_persons_id_attributes_returns_200(app_with_persons) -> None:
    """GET /persons/{id}/attributes returns 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-attrs', 'full_name': 'For attrs'},
        )
    assert create_resp.status_code == 201
    person_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/persons/{person_id}/attributes',
            json={'key': 'dept', 'value': 'Eng'},
        )
        attrs_resp = await client.get(f'/api/v0/persons/{person_id}/attributes')
    assert attrs_resp.status_code == 200
    attrs = attrs_resp.json()
    assert isinstance(attrs, list)
    assert len(attrs) >= 1


@pytest.mark.asyncio
async def test_get_persons_id_attributes_missing_person_returns_404(
    app_with_persons,
) -> None:
    """GET /persons/{id}/attributes returns 404 when person not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            f'/api/v0/persons/{uuid.uuid4()}/attributes',
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_persons_id_attributes_returns_201(app_with_persons) -> None:
    """POST /persons/{id}/attributes returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-postattr', 'full_name': 'Post attr'},
        )
    assert create_resp.status_code == 201
    person_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        attr_resp = await client.post(
            f'/api/v0/persons/{person_id}/attributes',
            json={'key': 'title', 'value': 'Engineer'},
        )
    assert attr_resp.status_code == 201
    data = attr_resp.json()
    assert data['key'] == 'title'
    assert data['value'] == 'Engineer'
    assert data['person_id'] == person_id


@pytest.mark.asyncio
async def test_post_persons_id_attributes_missing_person_returns_404(
    app_with_persons,
) -> None:
    """POST /persons/{id}/attributes returns 404 when person not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/persons/{uuid.uuid4()}/attributes',
            json={'key': 'title', 'value': 'Engineer'},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_persons_id_attributes_duplicate_key_returns_409(
    app_with_persons,
) -> None:
    """POST /persons/{id}/attributes with duplicate key returns 409."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-dup', 'full_name': 'Dup'},
        )
    assert create_resp.status_code == 201
    person_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/persons/{person_id}/attributes',
            json={'key': 'dupkey', 'value': 'v1'},
        )
        dup_resp = await client.post(
            f'/api/v0/persons/{person_id}/attributes',
            json={'key': 'dupkey', 'value': 'v2'},
        )
    assert dup_resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_persons_id_attributes_key_returns_204(app_with_persons) -> None:
    """DELETE /persons/{id}/attributes/{key} returns 204."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-del', 'full_name': 'Del'},
        )
    assert create_resp.status_code == 201
    person_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/persons/{person_id}/attributes',
            json={'key': 'to_delete', 'value': 'x'},
        )
        del_resp = await client.delete(
            f'/api/v0/persons/{person_id}/attributes/to_delete',
        )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_persons_id_attributes_key_missing_returns_404(
    app_with_persons,
) -> None:
    """DELETE /persons/{id}/attributes/{key} returns 404 when attribute not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/persons',
            json={'external_id': 'ext-nodel', 'full_name': 'No del'},
        )
    assert create_resp.status_code == 201
    person_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_persons),
        base_url='http://testserver',
    ) as client:
        response = await client.delete(
            f'/api/v0/persons/{person_id}/attributes/nonexistent',
        )
    assert response.status_code == 404
