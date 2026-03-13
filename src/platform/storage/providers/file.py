# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-based DataLakeStorage provider for local development."""

from collections.abc import Iterable
import json
import os
from pathlib import Path
from typing import Any
import uuid

from src.platform.storage.interface import DataLakeStorage

_DEFAULT_BASE = Path('.lake')


def _resolve_base_path() -> Path:
    raw = os.environ.get('AURELION_LAKE_PATH', '')
    if raw:
        return Path(raw)
    return Path.cwd() / _DEFAULT_BASE


def _sanitize_dataset_type(dataset_type: str) -> str:
    """Sanitize dataset_type for safe path component (no path traversal)."""
    if '..' in dataset_type or '/' in dataset_type or '\\' in dataset_type:
        raise ValueError(f'Invalid dataset_type for path: {dataset_type!r}')
    return dataset_type


class FileDataLakeStorage(DataLakeStorage):
    """File-based data lake backend. Stores JSONL batches under .lake/.

    This is the development backend for the data lake. Batches are written
    as one JSON object per line. Base path configurable via AURELION_LAKE_PATH.
    """

    def __init__(self, base_path: Path | None = None) -> None:
        self._base = base_path if base_path is not None else _resolve_base_path()

    def write_batch(
        self,
        dataset_type: str,
        records: Iterable[dict[str, Any]],
    ) -> str:
        key = str(uuid.uuid4())
        safe_type = _sanitize_dataset_type(dataset_type)
        file_path = self._base / safe_type / f'{key}.jsonl'
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, 'w', encoding='utf-8') as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')

        return f'{safe_type}/{key}'

    def read_batch(self, storage_key: str) -> Iterable[dict]:
        if '..' in storage_key or storage_key.startswith('/'):
            raise FileNotFoundError(f'Invalid storage_key: {storage_key!r}')
        file_path = self._base / f'{storage_key}.jsonl'
        if not file_path.exists():
            raise FileNotFoundError(f'Batch not found: {storage_key!r}')

        def _iterate() -> Iterable[dict]:
            with open(file_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        yield json.loads(line)

        return _iterate()

    def delete_batch(self, storage_key: str) -> None:
        if '..' in storage_key or storage_key.startswith('/'):
            raise FileNotFoundError(f'Invalid storage_key: {storage_key!r}')
        file_path = self._base / f'{storage_key}.jsonl'
        if not file_path.exists():
            raise FileNotFoundError(f'Batch not found: {storage_key!r}')
        file_path.unlink()
