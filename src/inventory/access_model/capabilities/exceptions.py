# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Capability slice domain exceptions."""

from __future__ import annotations


class CapabilityError(Exception):
    """Base class for all Capability slice errors."""


class CapabilitySlugAlreadyExistsError(CapabilityError):
    """Raised when a capability with the given slug already exists."""

    def __init__(self, slug: str) -> None:
        self.slug = slug
        super().__init__(f"Capability with slug '{slug}' already exists")


class CapabilityNotFoundError(CapabilityError):
    """Raised when a capability with the given id is not found."""

    def __init__(self, capability_id: int) -> None:
        self.capability_id = capability_id
        super().__init__(f'Capability {capability_id} not found')
