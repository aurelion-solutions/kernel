# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LakeBatchService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.lake_batches.service import BatchNotFoundError, LakeBatchService
from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.service import LogService
from src.platform.storage.factory import DataLakeStorageFactory, UnsupportedProviderError
from src.platform.storage.providers.file import FileDataLakeStorage


@pytest.fixture
def lake_path(tmp_path: Path) -> Path:
    return tmp_path / 'lake'


@pytest.fixture
def storage_factory(lake_path: Path) -> DataLakeStorageFactory:
    factory = DataLakeStorageFactory()
    factory.register('file', lambda: FileDataLakeStorage(base_path=lake_path))
    return factory


@pytest.fixture
def service(storage_factory: DataLakeStorageFactory) -> LakeBatchService:
    return LakeBatchService(storage_factory=storage_factory)


@pytest.mark.asyncio
async def test_create_batch_writes_records_to_lake(
    service: LakeBatchService,
    session_factory,
    lake_path: Path,
) -> None:
    """create_batch writes records to the lake."""
    records = [{'id': '1', 'name': 'a'}, {'id': '2', 'name': 'b'}]
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=records,
        )
        await session.commit()

    file_path = lake_path / f'{batch.storage_key}.jsonl'
    assert file_path.exists()
    with open(file_path) as f:
        lines = f.readlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {'id': '1', 'name': 'a'}


@pytest.mark.asyncio
async def test_create_batch_creates_metadata_row(
    service: LakeBatchService,
    session_factory,
) -> None:
    """create_batch creates metadata row in DB."""
    records = [{'x': 1}]
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='test',
            records=records,
        )
        await session.commit()

    async with session_factory() as session:
        loaded = await service.get_batch(session, batch.id)
    assert loaded is not None
    assert loaded.storage_provider == 'file'
    assert loaded.dataset_type == 'test'
    assert loaded.row_count == 1


@pytest.mark.asyncio
async def test_get_batch_returns_metadata_for_existing(
    service: LakeBatchService,
    session_factory,
) -> None:
    """get_batch returns metadata for existing batch."""
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=[{'a': 1}],
        )
        await session.commit()
        batch_id = batch.id

    async with session_factory() as session:
        loaded = await service.get_batch(session, batch_id)
    assert loaded is not None
    assert loaded.id == batch_id


@pytest.mark.asyncio
async def test_get_batch_returns_none_for_missing(
    service: LakeBatchService,
    session_factory,
) -> None:
    """get_batch returns None for missing batch."""
    async with session_factory() as session:
        result = await service.get_batch(session, uuid.uuid4())
    assert result is None


@pytest.mark.asyncio
async def test_read_batch_returns_records_from_lake(
    service: LakeBatchService,
    session_factory,
) -> None:
    """read_batch returns records from lake."""
    records = [{'id': '1'}, {'id': '2'}]
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=records,
        )
        await session.commit()
        batch_id = batch.id

    async with session_factory() as session:
        read = list(await service.read_batch(session, batch_id))
    assert read == records


@pytest.mark.asyncio
async def test_read_batch_raises_for_missing(
    service: LakeBatchService,
    session_factory,
) -> None:
    """read_batch raises BatchNotFoundError for missing batch."""
    async with session_factory() as session:
        with pytest.raises(BatchNotFoundError):
            list(await service.read_batch(session, uuid.uuid4()))


@pytest.mark.asyncio
async def test_delete_batch_removes_metadata_and_lake_payload(
    service: LakeBatchService,
    session_factory,
    lake_path: Path,
) -> None:
    """delete_batch removes metadata and lake payload."""
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='test',
            records=[{'x': 1}],
        )
        await session.commit()
        batch_id = batch.id
        storage_key = batch.storage_key

    async with session_factory() as session:
        await service.delete_batch(session, batch_id, delete_payload=True)
        await session.commit()

    assert not (lake_path / f'{storage_key}.jsonl').exists()
    async with session_factory() as session:
        loaded = await service.get_batch(session, batch_id)
    assert loaded is None


@pytest.mark.asyncio
async def test_delete_batch_delete_payload_false_removes_metadata_only(
    service: LakeBatchService,
    session_factory,
    lake_path: Path,
) -> None:
    """delete_batch(delete_payload=False) removes metadata only."""
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='test',
            records=[{'x': 1}],
        )
        await session.commit()
        batch_id = batch.id
        storage_key = batch.storage_key

    async with session_factory() as session:
        await service.delete_batch(session, batch_id, delete_payload=False)
        await session.commit()

    assert (lake_path / f'{storage_key}.jsonl').exists()
    async with session_factory() as session:
        loaded = await service.get_batch(session, batch_id)
    assert loaded is None


@pytest.mark.asyncio
async def test_delete_batch_raises_for_missing(
    service: LakeBatchService,
    session_factory,
) -> None:
    """delete_batch raises BatchNotFoundError for missing batch."""
    async with session_factory() as session:
        with pytest.raises(BatchNotFoundError):
            await service.delete_batch(session, uuid.uuid4())


@pytest.mark.asyncio
async def test_lake_batch_create_flow_emits_lake_batch_created_log(
    tmp_path: Path,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lake batch create flow emits a log event with lake.batch.created."""
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'file')
    lake_path = tmp_path / 'lake'
    log_path = tmp_path / 'logs.jsonl'
    storage_factory = DataLakeStorageFactory()
    storage_factory.register('file', lambda: FileDataLakeStorage(base_path=lake_path))
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory)
    service = LakeBatchService(
        storage_factory=storage_factory,
        log_service=log_service,
    )

    async with session_factory() as session:
        await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=[{'id': '1', 'name': 'a'}],
        )
        await session.commit()

    assert log_path.exists()
    lines = log_path.read_text().strip().split('\n')
    assert len(lines) >= 1
    records = [json.loads(line) for line in lines]
    created = [r for r in records if r.get('event_type') == 'lake.batch.created']
    assert len(created) == 1
    assert created[0]['component'] == 'data-lake'


@pytest.mark.asyncio
async def test_storage_resolution_failure_logs_and_re_raises(
    tmp_path: Path,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Storage resolution failure emits lake.provider.unsupported and re-raises."""
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'file')
    lake_path = tmp_path / 'lake'
    log_path = tmp_path / 'provider_fail.jsonl'
    storage_factory = DataLakeStorageFactory()
    storage_factory.register('file', lambda: FileDataLakeStorage(base_path=lake_path))
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(factory=log_factory)
    service = LakeBatchService(
        storage_factory=storage_factory,
        log_service=log_service,
    )

    with pytest.raises(
        UnsupportedProviderError,
        match=r"Unsupported storage provider: 'unknown'",
    ):
        async with session_factory() as session:
            await service.create_batch(
                session,
                storage_provider='unknown',
                dataset_type='test',
                records=[{'x': 1}],
            )

    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    failed = [r for r in records if r.get('event_type') == 'lake.provider.unsupported']
    assert len(failed) == 1
    assert failed[0]['payload']['storage_provider'] == 'unknown'
