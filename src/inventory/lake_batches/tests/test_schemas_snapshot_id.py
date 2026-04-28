# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for snapshot_id int64-as-string serialization on LakeBatchRead."""

from datetime import UTC, datetime
from pathlib import Path
import uuid

import pytest
from src.inventory.lake_batches.models import LakeBatch
from src.inventory.lake_batches.schemas import LakeBatchRead
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.storage.factory import DataLakeStorageFactory
from src.platform.storage.providers.file import FileDataLakeStorage

_NOW = datetime.now(UTC)

_SNAPSHOT_ID = 1234567890123456789


def _make_batch(snapshot_id: int | None = None) -> LakeBatch:
    return LakeBatch(
        id=uuid.uuid4(),
        dataset_type='test',
        row_count=0,
        created_at=_NOW,
        snapshot_id=snapshot_id,
    )


def test_snapshot_id_serialized_as_string_when_set() -> None:
    batch = _make_batch(snapshot_id=_SNAPSHOT_ID)
    schema = LakeBatchRead.model_validate(batch)
    dumped = schema.model_dump(mode='json')
    assert dumped['snapshot_id'] == str(_SNAPSHOT_ID)


def test_snapshot_id_serialized_as_null_when_unset() -> None:
    batch = _make_batch(snapshot_id=None)
    schema = LakeBatchRead.model_validate(batch)
    dumped = schema.model_dump(mode='json')
    assert dumped['snapshot_id'] is None


@pytest.mark.asyncio
async def test_snapshot_id_int_round_trip_via_get_id(engine, tmp_path: Path) -> None:
    """Full HTTP loop: POST batch with snapshot_id, GET by id → snapshot_id is string."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.core.db.deps import get_db
    from src.inventory.lake_batches.deps import get_lake_batch_service
    from src.inventory.lake_batches.repository import create_iceberg_lake_batch
    from src.inventory.lake_batches.routes import router as lake_batches_router

    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )
    lake_path = tmp_path / 'lake'
    factory = DataLakeStorageFactory()
    factory.register('file', lambda: FileDataLakeStorage(base_path=lake_path))
    service = LakeBatchService(storage_factory=factory)

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    def override_get_service():
        return service

    app = FastAPI()
    app.include_router(lake_batches_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_lake_batch_service] = override_get_service

    # Insert an Iceberg batch directly via repository (bypasses storage)
    async with session_factory() as session:
        batch = await create_iceberg_lake_batch(
            session,
            dataset_type='test',
            iceberg_namespace='raw',
            iceberg_table='access_artifacts',
            snapshot_id=_SNAPSHOT_ID,
            row_count=5,
        )
        await session.commit()
        batch_id = batch.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.get(f'/api/v0/datalake/batches/{batch_id}')

    assert resp.status_code == 200
    data = resp.json()
    assert data['snapshot_id'] == str(_SNAPSHOT_ID)
