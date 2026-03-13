# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for DataLakeStorageFactory."""

from pathlib import Path

import pytest
from src.platform.storage.factory import (
    DataLakeStorageFactory,
    UnsupportedProviderError,
    data_lake_factory,
)
from src.platform.storage.providers.file import FileDataLakeStorage
from src.platform.storage.providers.iceberg import IcebergStubDataLakeStorage
from src.platform.storage.providers.s3 import S3StubDataLakeStorage


def test_get_file_returns_file_data_lake_storage(tmp_path: Path) -> None:
    factory = DataLakeStorageFactory()
    factory.register('file', lambda: FileDataLakeStorage(base_path=tmp_path))
    storage = factory.get('file')
    assert isinstance(storage, FileDataLakeStorage)


def test_get_s3_returns_s3_stub() -> None:
    factory = DataLakeStorageFactory()
    storage = factory.get('s3')
    assert isinstance(storage, S3StubDataLakeStorage)


def test_get_iceberg_returns_iceberg_stub() -> None:
    factory = DataLakeStorageFactory()
    storage = factory.get('iceberg')
    assert isinstance(storage, IcebergStubDataLakeStorage)


def test_stub_methods_raise_not_implemented_error() -> None:
    factory = DataLakeStorageFactory()
    s3 = factory.get('s3')
    with pytest.raises(NotImplementedError, match='Stub provider not implemented'):
        s3.write_batch('test', [])
    with pytest.raises(NotImplementedError, match='Stub provider not implemented'):
        list(s3.read_batch('key'))
    with pytest.raises(NotImplementedError, match='Stub provider not implemented'):
        s3.delete_batch('key')


def test_get_unknown_raises_unsupported_provider_error() -> None:
    factory = DataLakeStorageFactory()
    with pytest.raises(UnsupportedProviderError, match="Unsupported storage provider: 'unknown'"):
        factory.get('unknown')


def test_list_names_returns_registered_providers() -> None:
    factory = DataLakeStorageFactory()
    names = factory.list_names()
    assert names == ['file', 'iceberg', 's3']


def test_data_lake_factory_singleton_get_file_returns_file_storage() -> None:
    """data_lake_factory.get('file') returns FileDataLakeStorage."""
    storage = data_lake_factory.get('file')
    assert isinstance(storage, FileDataLakeStorage)


def test_data_lake_factory_singleton_has_all_providers() -> None:
    """Module singleton has file, s3, iceberg registered."""
    assert data_lake_factory.get('file') is not None
    assert data_lake_factory.get('s3') is not None
    assert data_lake_factory.get('iceberg') is not None
    assert data_lake_factory.list_names() == ['file', 'iceberg', 's3']


def test_multiple_get_calls_return_independent_instances(tmp_path: Path) -> None:
    factory = DataLakeStorageFactory()
    factory.register('file', lambda: FileDataLakeStorage(base_path=tmp_path))
    a = factory.get('file')
    b = factory.get('file')
    assert a is not b
