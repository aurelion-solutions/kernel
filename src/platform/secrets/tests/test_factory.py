# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SecretManagerFactory."""

from pathlib import Path

import pytest
from src.platform.secrets.factory import (
    SecretManagerFactory,
    UnsupportedProviderError,
    secret_manager_factory,
)
from src.platform.secrets.interface import SecretManager
from src.platform.secrets.providers.file import FileSecretManager
from src.platform.secrets.providers.vault import VaultSecretManager


def test_get_file_returns_file_secret_manager(tmp_path: Path) -> None:
    factory = SecretManagerFactory()
    factory.register('file', lambda: FileSecretManager(path=tmp_path / 'secrets.json'))
    manager = factory.get('file')
    assert isinstance(manager, FileSecretManager)


def test_get_unknown_raises_unsupported_provider_error() -> None:
    factory = SecretManagerFactory()
    with pytest.raises(UnsupportedProviderError, match=r"Unsupported secret provider: 'unknown'"):
        factory.get('unknown')


def test_registered_provider_is_returned_by_get() -> None:
    factory = SecretManagerFactory()

    class StubManager(SecretManager):
        def set_secret(self, key: str, value: str) -> None:
            pass

        def get_secret(self, key: str) -> str:
            return ''

        def delete_secret(self, key: str) -> None:
            pass

    factory.register('stub', lambda: StubManager())
    manager = factory.get('stub')
    assert isinstance(manager, StubManager)


def test_multiple_get_calls_return_independent_instances(tmp_path: Path) -> None:
    factory = SecretManagerFactory()
    path = tmp_path / 'secrets.json'
    factory.register('file', lambda: FileSecretManager(path=path))
    a = factory.get('file')
    b = factory.get('file')
    assert a is not b


def test_module_singleton_has_file_registered() -> None:
    manager = secret_manager_factory.get('file')
    assert isinstance(manager, FileSecretManager)


def test_get_vault_returns_instance_get_secret_raises_not_implemented() -> None:
    manager = secret_manager_factory.get('vault')
    assert isinstance(manager, VaultSecretManager)
    with pytest.raises(NotImplementedError, match='Stub provider not implemented'):
        manager.get_secret('any_key')


def test_factory_file_provider_full_flow(tmp_path: Path) -> None:
    """Integration: factory → file provider → set → get → delete."""
    factory = SecretManagerFactory()
    factory.register('file', lambda: FileSecretManager(path=tmp_path / 'secrets.json'))
    manager = factory.get('file')
    manager.set_secret('test_key', 'test_value')
    assert manager.get_secret('test_key') == 'test_value'
    manager.delete_secret('test_key')
    with pytest.raises(KeyError, match=r"Secret not found: 'test_key'"):
        manager.get_secret('test_key')
