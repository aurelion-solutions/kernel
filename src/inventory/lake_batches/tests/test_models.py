# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LakeBatch model."""

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.lake_batches.models import LakeBatch


@pytest.mark.asyncio
async def test_create_lake_batch_with_required_fields(session_factory) -> None:
    """LakeBatch can be created with required fields."""
    async with session_factory() as session:
        batch = LakeBatch(
            storage_provider='file',
            dataset_type='accounts',
            storage_key='accounts/uuid-123',
            row_count=10,
        )
        session.add(batch)
        await session.flush()
        assert batch.id is not None
        assert batch.storage_provider == 'file'
        assert batch.dataset_type == 'accounts'
        assert batch.storage_key == 'accounts/uuid-123'
        assert batch.row_count == 10
        assert batch.created_at is not None


@pytest.mark.asyncio
async def test_persist_and_read_back_by_id(session_factory) -> None:
    """LakeBatch persists and can be read back by id."""
    async with session_factory() as session:
        batch = LakeBatch(
            storage_provider='file',
            dataset_type='resources',
            storage_key='resources/uuid-456',
            row_count=100,
        )
        session.add(batch)
        await session.commit()
        batch_id = batch.id

    async with session_factory() as session:
        loaded = await session.get(LakeBatch, batch_id)
        assert loaded is not None
        assert loaded.storage_provider == 'file'
        assert loaded.dataset_type == 'resources'
        assert loaded.storage_key == 'resources/uuid-456'
        assert loaded.row_count == 100


@pytest.mark.asyncio
async def test_optional_fields_may_be_null(session_factory) -> None:
    """Optional fields (application_id, task_id, content_type, metadata_json) may be null."""
    async with session_factory() as session:
        batch = LakeBatch(
            storage_provider='file',
            dataset_type='accounts',
            storage_key='accounts/uuid-789',
            row_count=0,
        )
        session.add(batch)
        await session.commit()

    assert batch.application_id is None
    assert batch.task_id is None
    assert batch.content_type is None
    assert batch.metadata_json is None


@pytest.mark.asyncio
async def test_duplicate_storage_provider_storage_key_rejected(session_factory) -> None:
    """Duplicate (storage_provider, storage_key) pair is rejected."""
    async with session_factory() as session:
        batch1 = LakeBatch(
            storage_provider='file',
            dataset_type='accounts',
            storage_key='accounts/dup-key',
            row_count=5,
        )
        session.add(batch1)
        await session.commit()

    async with session_factory() as session:
        batch2 = LakeBatch(
            storage_provider='file',
            dataset_type='accounts',
            storage_key='accounts/dup-key',
            row_count=10,
        )
        session.add(batch2)
        with pytest.raises(IntegrityError):
            await session.commit()
