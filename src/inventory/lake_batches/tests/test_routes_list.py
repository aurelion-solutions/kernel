# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for GET /api/v0/datalake/batches route."""

from pathlib import Path

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


async def _create_batch(client: AsyncClient) -> dict:
    resp = await client.post(
        '/api/v0/datalake/batches',
        json={
            'storage_provider': 'file',
            'dataset_type': 'test',
            'records': [{'x': 1}],
        },
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.asyncio
async def test_routes_list_returns_200_and_shape(app_with_lake) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        await _create_batch(client)
        response = await client.get('/api/v0/datalake/batches')
    assert response.status_code == 200
    data = response.json()
    assert 'items' in data
    assert 'next_cursor' in data
    assert isinstance(data['items'], list)
    assert len(data['items']) >= 1
    item = data['items'][0]
    assert 'id' in item
    assert 'dataset_type' in item
    assert 'row_count' in item


@pytest.mark.asyncio
async def test_routes_list_pagination_e2e(app_with_lake) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        for _ in range(3):
            await _create_batch(client)

        first = await client.get('/api/v0/datalake/batches?limit=2')
        assert first.status_code == 200
        first_data = first.json()
        assert len(first_data['items']) == 2
        assert first_data['next_cursor'] is not None

        second = await client.get(
            '/api/v0/datalake/batches',
            params={'limit': 2, 'cursor': first_data['next_cursor']},
        )
        assert second.status_code == 200
        second_data = second.json()
        assert len(second_data['items']) >= 1
        assert second_data['next_cursor'] is None

        # No overlap
        first_ids = {item['id'] for item in first_data['items']}
        second_ids = {item['id'] for item in second_data['items']}
        assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
async def test_routes_list_limit_clamp_below(app_with_lake) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/datalake/batches?limit=0')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_routes_list_limit_clamp_above(app_with_lake) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/datalake/batches?limit=500')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_routes_list_malformed_cursor_400(app_with_lake) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/datalake/batches?cursor=not_valid_cursor!!!')
    assert response.status_code == 400
    assert response.json()['detail'] == 'Invalid cursor'


@pytest.mark.asyncio
async def test_routes_list_empty_db_returns_empty_items(app_with_lake) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=app_with_lake),
        base_url='http://testserver',
    ) as client:
        response = await client.get('/api/v0/datalake/batches')
    assert response.status_code == 200
    data = response.json()
    assert data['items'] == []
    assert data['next_cursor'] is None
