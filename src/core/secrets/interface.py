# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Minimal secret reader interface for the config bootstrap layer.

Deliberately narrow: only ``get`` is required so the loader does not
depend on set/delete operations that live in src.platform.secrets.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ConfigSecretManager(Protocol):
    """Read-only secret interface for config bootstrap."""

    def get_secret(self, key: str) -> str:
        """Return the secret value for *key*.

        Raises ``KeyError`` when the key is absent.
        """
        ...
