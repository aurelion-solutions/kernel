# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for DataLakeStorage protocol."""

from src.platform.storage.interface import DataLakeStorage


def test_data_lake_storage_can_be_imported():
    """Interface can be imported and type-checked."""
    assert DataLakeStorage is not None


def test_mock_implementation_satisfies_protocol():
    """Minimal mock implementation satisfies the contract via structural subtyping."""

    class MockDataLakeStorage:
        def __init__(self) -> None:
            self._batches: dict[str, list[dict]] = {}

        def write_batch(self, dataset_type: str, records: list[dict]) -> str:
            key = f'{dataset_type}/mock-key'
            self._batches[key] = list(records)
            return key

        def read_batch(self, storage_key: str) -> list[dict]:
            return self._batches.get(storage_key, [])

        def delete_batch(self, storage_key: str) -> None:
            self._batches.pop(storage_key, None)

    mock = MockDataLakeStorage()
    assert isinstance(mock, DataLakeStorage)
