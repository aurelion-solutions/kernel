# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for FileSecretManager."""

from pathlib import Path
import tempfile

import pytest
from src.platform.secrets.providers.file import FileSecretManager


@pytest.fixture
def secrets_path(tmp_path: Path) -> Path:
    return tmp_path / 'secrets.json'


@pytest.fixture
def manager(secrets_path: Path) -> FileSecretManager:
    return FileSecretManager(path=secrets_path)


def test_set_secret_stores_value(manager: FileSecretManager) -> None:
    manager.set_secret('my_key', 'my_value')
    assert manager.get_secret('my_key') == 'my_value'


def test_get_secret_retrieves_stored_value(manager: FileSecretManager) -> None:
    manager.set_secret('foo', 'bar')
    assert manager.get_secret('foo') == 'bar'


def test_get_secret_missing_key_raises_key_error(manager: FileSecretManager) -> None:
    with pytest.raises(KeyError, match=r"Secret not found: 'missing'"):
        manager.get_secret('missing')


def test_set_secret_overwrites_existing_value(manager: FileSecretManager) -> None:
    manager.set_secret('key', 'first')
    manager.set_secret('key', 'second')
    assert manager.get_secret('key') == 'second'


def test_delete_secret_removes_key(manager: FileSecretManager) -> None:
    manager.set_secret('to_delete', 'value')
    manager.delete_secret('to_delete')
    with pytest.raises(KeyError, match=r"Secret not found: 'to_delete'"):
        manager.get_secret('to_delete')


def test_delete_secret_missing_key_raises_key_error(manager: FileSecretManager) -> None:
    with pytest.raises(KeyError, match=r"Secret not found: 'nonexistent'"):
        manager.delete_secret('nonexistent')


def test_file_secret_manager_satisfies_protocol() -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / 'secrets.json'
        manager = FileSecretManager(path=path)
        from src.platform.secrets.interface import SecretManager

        assert isinstance(manager, SecretManager)
