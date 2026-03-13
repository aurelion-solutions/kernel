# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-based secret provider for development only.

WARNING: This provider stores secrets in plain text. Do not use in production.
"""

import json
import os
from pathlib import Path
from threading import Lock

from src.platform.secrets.interface import SecretManager

_DEFAULT_PATH = Path('.secrets.json')


def _resolve_path() -> Path:
    raw = os.environ.get('AURELION_SECRETS_FILE', '')
    if raw:
        return Path(raw)
    return Path.cwd() / _DEFAULT_PATH


def _load(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _save(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True, indent=2)
    tmp.replace(path)


class FileSecretManager(SecretManager):
    """Development-only secret storage in a local JSON file.

    Secrets are stored in plain text. Safe for concurrent reads/writes
    within a single process via a lock. Uses atomic write for durability.

    Do not use in production.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _resolve_path()
        self._lock = Lock()

    def set_secret(self, key: str, value: str) -> None:
        with self._lock:
            data = _load(self._path)
            data[key] = value
            _save(self._path, data)

    def get_secret(self, key: str) -> str:
        with self._lock:
            data = _load(self._path)
            if key not in data:
                raise KeyError(f'Secret not found: {key!r}')
            return data[key]

    def delete_secret(self, key: str) -> None:
        with self._lock:
            data = _load(self._path)
            if key not in data:
                raise KeyError(f'Secret not found: {key!r}')
            del data[key]
            _save(self._path, data)
