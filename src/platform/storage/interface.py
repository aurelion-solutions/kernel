# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DataLakeStorage interface for lake storage backend abstraction."""

from collections.abc import Iterable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DataLakeStorage(Protocol):
    """Minimal contract for data lake batch storage. All lake backends must implement this."""

    def write_batch(
        self,
        dataset_type: str,
        records: Iterable[dict[str, Any]],
    ) -> str:
        """Write records as a batch. Returns storage_key for later retrieval."""
        ...

    def read_batch(self, storage_key: str) -> Iterable[dict[str, Any]]:
        """Read records by storage_key. Returns iterable of record dicts."""
        ...

    def delete_batch(self, storage_key: str) -> None:
        """Remove batch by storage_key."""
        ...
