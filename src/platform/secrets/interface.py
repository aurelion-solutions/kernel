# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SecretManager interface for secret provider abstraction."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretManager(Protocol):
    """Minimal contract for secret storage. All providers must implement this."""

    def set_secret(self, key: str, value: str) -> None:
        """Store a secret under the given key."""
        ...

    def get_secret(self, key: str) -> str:
        """Retrieve a secret by key."""
        ...

    def delete_secret(self, key: str) -> None:
        """Remove a secret by key."""
        ...
