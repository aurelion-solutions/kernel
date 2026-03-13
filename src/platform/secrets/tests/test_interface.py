# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SecretManager protocol."""

from src.platform.secrets.interface import SecretManager


def test_secret_manager_can_be_imported():
    """Protocol can be imported and type-checked."""
    assert SecretManager is not None


def test_mock_implementation_satisfies_protocol():
    """Minimal mock implementation satisfies the contract via structural subtyping."""

    class MockSecretManager:
        def __init__(self) -> None:
            self._store: dict[str, str] = {}

        def set_secret(self, key: str, value: str) -> None:
            self._store[key] = value

        def get_secret(self, key: str) -> str:
            return self._store[key]

        def delete_secret(self, key: str) -> None:
            del self._store[key]

    mock = MockSecretManager()
    assert isinstance(mock, SecretManager)
