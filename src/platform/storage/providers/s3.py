# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""S3 object storage stub. Placeholder for future implementation."""

from collections.abc import Iterable
from typing import Any

from src.platform.storage.interface import DataLakeStorage


class S3StubDataLakeStorage(DataLakeStorage):
    """S3-compatible object storage stub."""

    def write_batch(
        self,
        dataset_type: str,
        records: Iterable[dict[str, Any]],
    ) -> str:
        raise NotImplementedError('Stub provider not implemented')

    def read_batch(self, storage_key: str) -> Iterable[dict[str, Any]]:
        raise NotImplementedError('Stub provider not implemented')

    def delete_batch(self, storage_key: str) -> None:
        raise NotImplementedError('Stub provider not implemented')
