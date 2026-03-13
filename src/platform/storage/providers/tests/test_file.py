# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for FileDataLakeStorage provider."""

from pathlib import Path

import pytest
from src.platform.storage.interface import DataLakeStorage
from src.platform.storage.providers.file import FileDataLakeStorage


@pytest.fixture
def lake_path(tmp_path: Path) -> Path:
    return tmp_path / 'lake'


@pytest.fixture
def storage(lake_path: Path) -> FileDataLakeStorage:
    return FileDataLakeStorage(base_path=lake_path)


def test_write_batch_returns_storage_key(storage: FileDataLakeStorage) -> None:
    """write_batch returns a storage_key."""
    records = [{'id': '1', 'name': 'a'}, {'id': '2', 'name': 'b'}]
    key = storage.write_batch('accounts', records)
    assert key
    assert '/' in key
    assert key.startswith('accounts/')


def test_read_batch_returns_same_records(storage: FileDataLakeStorage) -> None:
    """read_batch returns the same records that were written."""
    records = [{'id': '1', 'name': 'alice'}, {'id': '2', 'name': 'bob'}]
    key = storage.write_batch('accounts', records)
    read = list(storage.read_batch(key))
    assert read == records


def test_delete_batch_removes_data(
    storage: FileDataLakeStorage,
    lake_path: Path,
) -> None:
    """delete_batch removes the file."""
    records = [{'x': 1}]
    key = storage.write_batch('test', records)
    file_path = lake_path / f'{key}.jsonl'
    assert file_path.exists()

    storage.delete_batch(key)
    assert not file_path.exists()


def test_read_batch_on_deleted_key_raises(storage: FileDataLakeStorage) -> None:
    """read_batch on deleted key raises FileNotFoundError."""
    key = storage.write_batch('test', [{'a': 1}])
    storage.delete_batch(key)

    with pytest.raises(FileNotFoundError, match='Batch not found'):
        list(storage.read_batch(key))


def test_read_batch_missing_key_raises(storage: FileDataLakeStorage) -> None:
    """read_batch on missing storage_key raises clear error."""
    with pytest.raises(FileNotFoundError, match='Batch not found'):
        list(storage.read_batch('accounts/nonexistent-uuid'))


def test_delete_batch_missing_key_raises(storage: FileDataLakeStorage) -> None:
    """delete_batch on missing storage_key raises clear error."""
    with pytest.raises(FileNotFoundError, match='Batch not found'):
        storage.delete_batch('accounts/nonexistent-uuid')


def test_large_batch_smoke(storage: FileDataLakeStorage) -> None:
    """Write and read 10k records."""
    records = [{'i': i, 'data': f'row-{i}'} for i in range(10_000)]
    key = storage.write_batch('bulk', records)

    read = list(storage.read_batch(key))
    assert len(read) == 10_000
    assert read[0] == {'i': 0, 'data': 'row-0'}
    assert read[9999] == {'i': 9999, 'data': 'row-9999'}


def test_different_dataset_types_produce_different_dirs(
    storage: FileDataLakeStorage,
    lake_path: Path,
) -> None:
    """Different dataset_type values produce different subdirectories."""
    storage.write_batch('accounts', [{'a': 1}])
    storage.write_batch('resources', [{'r': 1}])

    assert (lake_path / 'accounts').is_dir()
    assert (lake_path / 'resources').is_dir()
    assert len(list((lake_path / 'accounts').glob('*.jsonl'))) == 1
    assert len(list((lake_path / 'resources').glob('*.jsonl'))) == 1


def test_storage_implements_protocol(storage: FileDataLakeStorage) -> None:
    """FileDataLakeStorage satisfies DataLakeStorage protocol."""
    assert isinstance(storage, DataLakeStorage)
