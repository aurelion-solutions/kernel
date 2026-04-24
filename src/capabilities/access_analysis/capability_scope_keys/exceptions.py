# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityScopeKey slice domain exceptions."""

from __future__ import annotations


class CapabilityScopeKeyError(Exception):
    """Base class for all CapabilityScopeKey slice errors."""


class CapabilityScopeKeyCodeAlreadyExistsError(CapabilityScopeKeyError):
    """Raised when a capability scope key with the given code already exists."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Capability scope key with code '{code}' already exists")


class CapabilityScopeKeyNotFoundError(CapabilityScopeKeyError):
    """Raised when a capability scope key with the given id is not found."""

    def __init__(self, scope_key_id: int) -> None:
        self.scope_key_id = scope_key_id
        super().__init__(f'Capability scope key {scope_key_id} not found')
