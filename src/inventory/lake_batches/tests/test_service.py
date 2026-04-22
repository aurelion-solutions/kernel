# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LakeBatchService."""

import json
from pathlib import Path
import uuid

import pytest
from src.inventory.lake_batches.service import BatchNotFoundError, LakeBatchService
from src.platform.events.schemas import EventParticipantKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
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
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


@pytest.fixture
def service(
    storage_factory: DataLakeStorageFactory,
    event_service: EventService,
) -> LakeBatchService:
    return LakeBatchService(
        storage_factory=storage_factory,
        event_service=event_service,
        # log_service omitted → falls back to noop_log_service
    )


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
async def test_create_batch_emits_inventory_lake_batch_created_event(
    service: LakeBatchService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """create_batch emits inventory.lake_batch.created via EventService."""
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=[{'id': '1'}],
        )
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.lake_batch.created')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.lake_batches'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(batch.id)
    assert envelope.payload['batch_id'] == str(batch.id)
    assert envelope.payload['storage_provider'] == 'file'
    assert envelope.payload['dataset_type'] == 'accounts'
    assert envelope.payload['storage_key'] == batch.storage_key
    assert envelope.payload['row_count'] == 1


@pytest.mark.asyncio
async def test_delete_batch_emits_inventory_lake_batch_deleted_event(
    service: LakeBatchService,
    capturing_events: CapturingEventService,
    session_factory,
) -> None:
    """delete_batch emits inventory.lake_batch.deleted via EventService."""
    async with session_factory() as session:
        batch = await service.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=[{'id': '1'}],
        )
        await session.commit()
        batch_id = batch.id

    capturing_events.emitted.clear()

    async with session_factory() as session:
        await service.delete_batch(session, batch_id, delete_payload=False)
        await session.commit()

    emitted = capturing_events.filter_by_type('inventory.lake_batch.deleted')
    assert len(emitted) == 1
    envelope = emitted[0]
    assert envelope.actor_kind == EventParticipantKind.CAPABILITY
    assert envelope.actor_id == 'inventory.lake_batches'
    assert envelope.target_kind == EventParticipantKind.SYSTEM
    assert envelope.target_id == str(batch_id)
    assert envelope.payload['batch_id'] == str(batch_id)
    assert envelope.payload['storage_provider'] == 'file'
    assert envelope.payload['storage_key'] == batch.storage_key
    assert 'row_count' not in envelope.payload


@pytest.mark.asyncio
async def test_storage_resolution_failure_logs_without_event_type_and_re_raises(
    tmp_path: Path,
    session_factory,
) -> None:
    """Storage resolution failure emits ERROR log without event_type and re-raises."""
    lake_path = tmp_path / 'lake'
    log_path = tmp_path / 'provider_fail.jsonl'
    storage_factory = DataLakeStorageFactory()
    storage_factory.register('file', lambda: FileDataLakeStorage(base_path=lake_path))
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(sink=log_factory.get('file'))
    svc = LakeBatchService(
        storage_factory=storage_factory,
        log_service=log_service,
    )

    with pytest.raises(
        UnsupportedProviderError,
        match=r"Unsupported storage provider: 'unknown'",
    ):
        async with session_factory() as session:
            await svc.create_batch(
                session,
                storage_provider='unknown',
                dataset_type='test',
                records=[{'x': 1}],
            )

    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    error_records = [r for r in records if r.get('level') == 'error']
    assert len(error_records) >= 1
    failed = error_records[0]
    assert failed['component'] == 'data-lake'
    assert failed['payload']['storage_provider'] == 'unknown'
    # KEEP-variant anti-dual-emit: operational log must NOT carry event_type
    assert 'event_type' not in failed


@pytest.mark.asyncio
async def test_create_batch_does_not_emit_legacy_log_event_types(
    tmp_path: Path,
    session_factory,
) -> None:
    """KEEP-variant anti-dual-emit guard: create_batch log records have no event_type key."""
    lake_path = tmp_path / 'lake'
    log_path = tmp_path / 'logs.jsonl'
    storage_factory = DataLakeStorageFactory()
    storage_factory.register('file', lambda: FileDataLakeStorage(base_path=lake_path))
    log_factory = LogSinkFactory()
    log_factory.register('file', lambda: FileLogSink(path=log_path))
    log_service = LogService(sink=log_factory.get('file'))

    capturing = CapturingEventService()
    event_svc = EventService(sink=capturing)
    svc = LakeBatchService(
        storage_factory=storage_factory,
        log_service=log_service,
        event_service=event_svc,
    )

    async with session_factory() as session:
        await svc.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=[{'id': '1'}],
        )
        await session.commit()

    # New event emitted on event bus
    assert len(capturing.filter_by_type('inventory.lake_batch.created')) == 1

    # Log records must not carry any event_type (KEEP-variant guard)
    assert log_path.exists()
    log_records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    for record in log_records:
        assert 'event_type' not in record, f'Unexpected event_type in log record: {record}'
    # Confirm neither legacy nor new event_type leaked into log bus
    assert [r for r in log_records if r.get('event_type') == 'lake.batch.created'] == []
    assert [r for r in log_records if r.get('event_type') == 'inventory.lake_batch.created'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'method,explicit_corr_id',
    [
        ('create_batch', 'trace-lake-xyz'),
        ('create_batch', None),
        ('delete_batch', 'trace-lake-xyz'),
        ('delete_batch', None),
    ],
)
async def test_correlation_id_propagates_to_created_and_deleted_events(
    method: str,
    explicit_corr_id: str | None,
    storage_factory: DataLakeStorageFactory,
    capturing_events: CapturingEventService,
    event_service: EventService,
    session_factory,
) -> None:
    """correlation_id kwarg is forwarded to emitted event envelope."""
    svc = LakeBatchService(storage_factory=storage_factory, event_service=event_service)

    async with session_factory() as session:
        batch = await svc.create_batch(
            session,
            storage_provider='file',
            dataset_type='accounts',
            records=[{'id': '1'}],
            correlation_id=explicit_corr_id if method == 'create_batch' else None,
        )
        await session.commit()
        batch_id = batch.id

    if method == 'delete_batch':
        capturing_events.emitted.clear()
        async with session_factory() as session:
            await svc.delete_batch(
                session,
                batch_id,
                delete_payload=False,
                correlation_id=explicit_corr_id,
            )
            await session.commit()
        event_type = 'inventory.lake_batch.deleted'
    else:
        event_type = 'inventory.lake_batch.created'

    emitted = capturing_events.filter_by_type(event_type)
    assert len(emitted) == 1
    envelope = emitted[0]
    assert isinstance(envelope.correlation_id, str)

    if explicit_corr_id is not None:
        assert envelope.correlation_id == explicit_corr_id
    else:
        # auto-generated: uuid4().hex shape — 32 lowercase hex chars
        assert len(envelope.correlation_id) == 32
        assert envelope.correlation_id == envelope.correlation_id.lower()
        assert all(c in '0123456789abcdef' for c in envelope.correlation_id)
