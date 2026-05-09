# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for lake batch API routes."""

from pathlib import Path
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.storage.factory import DataLakeStorageFactory
from src.platform.storage.providers.file import FileDataLakeStorage


@pytest.fixture
def lake_path(tmp_path: Path) -> Path:
    return tmp_path / 'lake'


@pytest.fixture
def app_with_lake(engine, lake_path: Path):
    """App with lake batch routes and file storage using temp path."""
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.core.db.deps import get_db
    from src.inventory.lake_batches.deps import get_lake_batch_service
    from src.inventory.lake_batches.routes import router as lake_batches_router

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
    factory = DataLakeStorageFactory()
    factory.register('file', lambda: FileDataLakeStorage(base_path=lake_path))
    service = LakeBatchService(storage_factory=factory)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                await session.rollback()
                raise

    def override_get_service():
        return service

    app = FastAPI()
    app.include_router(lake_batches_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_lake_batch_service] = override_get_service
    return app


@pytest.mark.asyncio
async def test_post_lake_batches_valid_body_returns_201(app_with_lake) -> None:
    """POST /lake-batches with valid body returns 201."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/datalake/batches',
            json={
                'storage_provider': 'file',
                'dataset_type': 'accounts',
                'records': [{'id': '1', 'name': 'a'}],
            },
        )
    assert response.status_code == 201
    data = response.json()
    assert 'id' in data
    assert data['storage_provider'] == 'file'
    assert data['dataset_type'] == 'accounts'
    assert data['row_count'] == 1


@pytest.mark.asyncio
async def test_post_lake_batches_unknown_provider_returns_400(app_with_lake) -> None:
    """POST /lake-batches with unknown storage_provider returns 400."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/datalake/batches',
            json={
                'storage_provider': 'unknown',
                'dataset_type': 'accounts',
                'records': [{'x': 1}],
            },
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_get_lake_batches_id_returns_200(app_with_lake) -> None:
    """GET /lake-batches/{id} returns 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/datalake/batches',
            json={
                'storage_provider': 'file',
                'dataset_type': 'test',
                'records': [{'x': 1}],
            },
        )
    assert create_resp.status_code == 201
    batch_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/datalake/batches/{batch_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['id'] == batch_id


@pytest.mark.asyncio
async def test_get_lake_batches_id_missing_returns_404(app_with_lake) -> None:
    """GET /lake-batches/{id} missing returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/datalake/batches/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_get_lake_batches_id_data_returns_200(app_with_lake) -> None:
    """GET /lake-batches/{id}/data returns 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/datalake/batches',
            json={
                'storage_provider': 'file',
                'dataset_type': 'test',
                'records': [{'a': 1}, {'b': 2}],
            },
        )
    assert create_resp.status_code == 201
    batch_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        data_resp = await client.get(f'/api/v0/datalake/batches/{batch_id}/data')
    assert data_resp.status_code == 200
    assert data_resp.json() == [{'a': 1}, {'b': 2}]


@pytest.mark.asyncio
async def test_get_lake_batches_id_data_missing_returns_404(app_with_lake) -> None:
    """GET /lake-batches/{id}/data missing returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.get(f'/api/v0/datalake/batches/{uuid.uuid4()}/data')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_delete_lake_batches_id_returns_204(app_with_lake) -> None:
    """DELETE /lake-batches/{id} returns 204."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/datalake/batches',
            json={
                'storage_provider': 'file',
                'dataset_type': 'test',
                'records': [{'x': 1}],
            },
        )
    assert create_resp.status_code == 201
    batch_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        delete_resp = await client.delete(f'/api/v0/datalake/batches/{batch_id}')
    assert delete_resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_lake_batches_id_missing_returns_404(app_with_lake) -> None:
    """DELETE /lake-batches/{id} missing returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.delete(f'/api/v0/datalake/batches/{uuid.uuid4()}')
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_full_flow_post_get_metadata_get_data_delete_get_404(app_with_lake) -> None:
    """Full flow: POST → GET metadata → GET data → DELETE → GET metadata 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/datalake/batches',
            json={
                'storage_provider': 'file',
                'dataset_type': 'accounts',
                'records': [{'id': '1'}, {'id': '2'}],
            },
        )
    assert create_resp.status_code == 201
    batch_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        get_resp = await client.get(f'/api/v0/datalake/batches/{batch_id}')
    assert get_resp.status_code == 200
    assert get_resp.json()['row_count'] == 2

    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        data_resp = await client.get(f'/api/v0/datalake/batches/{batch_id}/data')
    assert data_resp.status_code == 200
    assert data_resp.json() == [{'id': '1'}, {'id': '2'}]

    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        delete_resp = await client.delete(f'/api/v0/datalake/batches/{batch_id}')
    assert delete_resp.status_code == 204

    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        get_after = await client.get(f'/api/v0/datalake/batches/{batch_id}')
    assert get_after.status_code == 404
