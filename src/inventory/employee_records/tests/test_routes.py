# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeRecord API routes."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.inventory.employee_records.routes import router as employee_records_router
from src.platform.applications.models import Application


@pytest.fixture
def app_with_employee_records(engine):
    """App with employee record routes using test engine."""
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
    app.include_router(employee_records_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    return app


@pytest.fixture
async def application_id_for_records(engine):
    """Create an application for employee record tests."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
    async with session_factory() as session:
        app = Application(name='hr-records', code='hr-records')
        session.add(app)
        await session.commit()
        return app.id


@pytest.mark.asyncio
async def test_post_employee_records_returns_201(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """POST /employee-records with valid body returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-1',
                'application_id': str(application_id_for_records),
                'description': 'Alice',
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['external_id'] == 'rec-1'
    assert data['application_id'] == str(application_id_for_records)
    assert data['description'] == 'Alice'


@pytest.mark.asyncio
async def test_post_employee_records_invalid_application_id_returns_404(
    app_with_employee_records,
) -> None:
    """POST /employee-records with invalid application_id returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-bad',
                'application_id': str(uuid.uuid4()),
            },
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_employee_records_returns_200(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """GET /employee-records returns 200 and list."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-list',
                'application_id': str(application_id_for_records),
            },
        )
        response = await client.get('/api/v0/employee-records')
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) >= 1


@pytest.mark.asyncio
async def test_get_employee_records_id_returns_200(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """GET /employee-records/{id} returns 200 when found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-get',
                'application_id': str(application_id_for_records),
            },
        )
    assert create_resp.status_code == 201
    record_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/employee-records/{record_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == record_id


@pytest.mark.asyncio
async def test_get_employee_records_id_missing_returns_404(
    app_with_employee_records,
) -> None:
    """GET /employee-records/{id} returns 404 when not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/employee-records/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_employee_records_id_attributes_returns_200(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """GET /employee-records/{id}/attributes returns 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-attrs',
                'application_id': str(application_id_for_records),
            },
        )
    assert create_resp.status_code == 201
    record_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/employee-records/{record_id}/attributes',
            json={'key': 'dept', 'value': 'Eng'},
        )
        attrs_resp = await client.get(f'/api/v0/employee-records/{record_id}/attributes')
    assert attrs_resp.status_code == 200
    attrs = attrs_resp.json()
    assert isinstance(attrs, list)
    assert len(attrs) >= 1


@pytest.mark.asyncio
async def test_get_employee_records_id_attributes_missing_record_returns_404(
    app_with_employee_records,
) -> None:
    """GET /employee-records/{id}/attributes returns 404 when record not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        response = await client.get(
            f'/api/v0/employee-records/{uuid.uuid4()}/attributes',
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_employee_records_id_attributes_returns_201(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """POST /employee-records/{id}/attributes returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-postattr',
                'application_id': str(application_id_for_records),
            },
        )
    assert create_resp.status_code == 201
    record_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        attr_resp = await client.post(
            f'/api/v0/employee-records/{record_id}/attributes',
            json={'key': 'title', 'value': 'Engineer'},
        )
    assert attr_resp.status_code == 201
    data = attr_resp.json()
    assert data['key'] == 'title'
    assert data['value'] == 'Engineer'
    assert data['employee_record_id'] == record_id


@pytest.mark.asyncio
async def test_post_employee_records_id_attributes_missing_record_returns_404(
    app_with_employee_records,
) -> None:
    """POST /employee-records/{id}/attributes returns 404 when record not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/employee-records/{uuid.uuid4()}/attributes',
            json={'key': 'title', 'value': 'Engineer'},
        )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_post_employee_records_id_attributes_duplicate_key_returns_409(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """POST /employee-records/{id}/attributes with duplicate key returns 409."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-dup',
                'application_id': str(application_id_for_records),
            },
        )
    assert create_resp.status_code == 201
    record_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/employee-records/{record_id}/attributes',
            json={'key': 'dupkey', 'value': 'v1'},
        )
        dup_resp = await client.post(
            f'/api/v0/employee-records/{record_id}/attributes',
            json={'key': 'dupkey', 'value': 'v2'},
        )
    assert dup_resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_employee_records_id_attributes_key_returns_204(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """DELETE /employee-records/{id}/attributes/{key} returns 204."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-del',
                'application_id': str(application_id_for_records),
            },
        )
    assert create_resp.status_code == 201
    record_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        await client.post(
            f'/api/v0/employee-records/{record_id}/attributes',
            json={'key': 'to_delete', 'value': 'x'},
        )
        del_resp = await client.delete(
            f'/api/v0/employee-records/{record_id}/attributes/to_delete',
        )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_employee_records_id_attributes_key_missing_returns_404(
    app_with_employee_records,
    application_id_for_records: uuid.UUID,
) -> None:
    """DELETE /employee-records/{id}/attributes/{key} returns 404 when attribute not found."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/employee-records',
            json={
                'external_id': 'rec-nodel',
                'application_id': str(application_id_for_records),
            },
        )
    assert create_resp.status_code == 201
    record_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_employee_records),
        base_url='http://testserver',
    ) as client:
        response = await client.delete(
            f'/api/v0/employee-records/{record_id}/attributes/nonexistent',
        )
    assert response.status_code == 404
