# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LakeBatch schemas."""

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.lake_batches.models import LakeBatch
from src.inventory.lake_batches.schemas import LakeBatchCreate, LakeBatchRead

_NOW = datetime.now(UTC)


def test_lake_batch_create_accepts_valid_input() -> None:
    schema = LakeBatchCreate(
        storage_provider='file',
        dataset_type='accounts',
        storage_key='accounts/uuid-123',
        row_count=10,
    )
    assert schema.storage_provider == 'file'
    assert schema.dataset_type == 'accounts'
    assert schema.storage_key == 'accounts/uuid-123'
    assert schema.row_count == 10


def test_lake_batch_create_rejects_negative_row_count() -> None:
    with pytest.raises(ValueError):
        LakeBatchCreate(
            storage_provider='file',
            dataset_type='accounts',
            storage_key='accounts/uuid-123',
            row_count=-1,
        )


def test_lake_batch_create_rejects_empty_storage_provider() -> None:
    with pytest.raises(ValueError):
        LakeBatchCreate(
            storage_provider='',
            dataset_type='accounts',
            storage_key='accounts/uuid-123',
            row_count=0,
        )


def test_lake_batch_create_rejects_empty_dataset_type() -> None:
    with pytest.raises(ValueError):
        LakeBatchCreate(
            storage_provider='file',
            dataset_type='',
            storage_key='accounts/uuid-123',
            row_count=0,
        )


def test_lake_batch_create_rejects_empty_storage_key() -> None:
    with pytest.raises(ValueError):
        LakeBatchCreate(
            storage_provider='file',
            dataset_type='accounts',
            storage_key='',
            row_count=0,
        )


def test_lake_batch_read_from_orm_instance() -> None:
    batch = LakeBatch(
        id=uuid.uuid4(),
        storage_provider='file',
        dataset_type='resources',
        storage_key='resources/uuid-456',
        row_count=100,
        created_at=_NOW,
    )
    schema = LakeBatchRead.model_validate(batch)
    assert schema.id == batch.id
    assert schema.storage_provider == 'file'
    assert schema.dataset_type == 'resources'
    assert schema.storage_key == 'resources/uuid-456'
    assert schema.row_count == 100
    assert schema.created_at == batch.created_at


def test_lake_batch_read_includes_expected_fields() -> None:
    batch = LakeBatch(
        id=uuid.uuid4(),
        storage_provider='s3',
        dataset_type='accounts',
        storage_key='accounts/key',
        row_count=5,
        created_at=_NOW,
        application_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        content_type='application/json',
        metadata_json={'source': 'connector'},
    )
    schema = LakeBatchRead.model_validate(batch)
    assert hasattr(schema, 'id')
    assert hasattr(schema, 'storage_provider')
    assert hasattr(schema, 'dataset_type')
    assert hasattr(schema, 'storage_key')
    assert hasattr(schema, 'row_count')
    assert hasattr(schema, 'created_at')
    assert hasattr(schema, 'application_id')
    assert hasattr(schema, 'task_id')
    assert hasattr(schema, 'content_type')
    assert hasattr(schema, 'metadata_json')
    assert schema.application_id is not None
    assert schema.task_id is not None
    assert schema.content_type == 'application/json'
    assert schema.metadata_json == {'source': 'connector'}
