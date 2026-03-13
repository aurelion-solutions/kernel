# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Customer API routes."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.customers.routes import router as customers_router


@pytest.fixture
def app_with_customers(engine):
    """App with customer routes using test engine."""
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
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(customers_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.mark.asyncio
async def test_post_customers_returns_201(app_with_customers) -> None:
    """POST /customers with valid body returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-ext-001'},
        )
    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['external_id'] == 'route-ext-001'
    assert data['email_verified'] is False
    assert data['mfa_enabled'] is True
    assert data['is_locked'] is False
    assert 'created_at' in data
    assert 'updated_at' in data


@pytest.mark.asyncio
async def test_post_customers_with_plan_tier(app_with_customers) -> None:
    """POST /customers with plan_tier and tenant_role succeeds."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/customers',
            json={
                'external_id': 'route-ext-002',
                'plan_tier': 'pro',
                'tenant_role': 'admin',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert data['plan_tier'] == 'pro'
    assert data['tenant_role'] == 'admin'


@pytest.mark.asyncio
async def test_get_customers_returns_list(app_with_customers) -> None:
    """GET /customers returns 200 and a list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        await client.post('/api/v0/customers', json={'external_id': 'route-list-001'})
        response = await client.get('/api/v0/customers')
    assert response.status_code == 200
    assert isinstance(response.json(), list)
    assert len(response.json()) >= 1


@pytest.mark.asyncio
async def test_get_customers_filter_is_locked(app_with_customers) -> None:
    """GET /customers?is_locked=true returns only locked customers."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-locked-001', 'is_locked': True},
        )
        await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-unlocked-001', 'is_locked': False},
        )
        response = await client.get('/api/v0/customers?is_locked=true')
    assert response.status_code == 200
    data = response.json()
    assert all(c['is_locked'] is True for c in data)


@pytest.mark.asyncio
async def test_get_customer_by_id_returns_200(app_with_customers) -> None:
    """GET /customers/{id} returns 200 when found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-get-001'},
        )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/customers/{customer_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == customer_id


@pytest.mark.asyncio
async def test_get_customer_missing_returns_404(app_with_customers) -> None:
    """GET /customers/{id} returns 404 when not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/customers/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_customer_changes_is_locked(app_with_customers) -> None:
    """PATCH /customers/{id} updates is_locked."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-patch-001'},
        )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        patch_resp = await client.patch(
            f'/api/v0/customers/{customer_id}',
            json={'is_locked': True},
        )
    assert patch_resp.status_code == 200
    assert patch_resp.json()['is_locked'] is True


@pytest.mark.asyncio
async def test_patch_customer_missing_returns_404(app_with_customers) -> None:
    """PATCH /customers/{id} returns 404 when not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        response = await client.patch(
            f'/api/v0/customers/{uuid.uuid4()}',
            json={'is_locked': True},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_customer_attributes_returns_201(app_with_customers) -> None:
    """POST /customers/{id}/attributes returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-attr-001'},
        )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        attr_resp = await client.post(
            f'/api/v0/customers/{customer_id}/attributes',
            json={'key': 'country', 'value': 'US'},
        )
    assert attr_resp.status_code == 201
    data = attr_resp.json()
    assert data['key'] == 'country'
    assert data['value'] == 'US'
    assert data['customer_id'] == customer_id
    assert 'created_at' in data


@pytest.mark.asyncio
async def test_post_customer_attributes_duplicate_key_returns_409(
    app_with_customers,
) -> None:
    """POST /customers/{id}/attributes with duplicate key returns 409."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-dup-001'},
        )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/customers/{customer_id}/attributes',
            json={'key': 'dupkey', 'value': 'v1'},
        )
        dup_resp = await client.post(
            f'/api/v0/customers/{customer_id}/attributes',
            json={'key': 'dupkey', 'value': 'v2'},
        )
    assert dup_resp.status_code == 409


@pytest.mark.asyncio
async def test_post_customer_attributes_missing_customer_returns_404(
    app_with_customers,
) -> None:
    """POST /customers/{id}/attributes returns 404 when customer not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/customers/{uuid.uuid4()}/attributes',
            json={'key': 'k', 'value': 'v'},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_customer_attribute_returns_204(app_with_customers) -> None:
    """DELETE /customers/{id}/attributes/{key} returns 204."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-del-001'},
        )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/customers/{customer_id}/attributes',
            json={'key': 'to_delete', 'value': 'x'},
        )
        del_resp = await client.delete(
            f'/api/v0/customers/{customer_id}/attributes/to_delete',
        )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_get_customer_attributes_returns_list(app_with_customers) -> None:
    """GET /customers/{id}/attributes returns list of attributes."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/customers',
            json={'external_id': 'route-attrs-001'},
        )
    assert create_resp.status_code == 201
    customer_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_customers),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/customers/{customer_id}/attributes',
            json={'key': 'region', 'value': 'eu-west'},
        )
        attrs_resp = await client.get(f'/api/v0/customers/{customer_id}/attributes')
    assert attrs_resp.status_code == 200
    data = attrs_resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
