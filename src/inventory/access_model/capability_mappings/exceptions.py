# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityMapping domain exceptions."""

from __future__ import annotations

from uuid import UUID


class CapabilityMappingError(Exception):
    """Base exception for all CapabilityMapping errors."""


class CapabilityMappingNotFoundError(CapabilityMappingError):
    def __init__(self, mapping_id: int) -> None:
        self.mapping_id = mapping_id
        super().__init__(f'CapabilityMapping {mapping_id} not found')


class CapabilityMappingResourceMatchExclusivityError(CapabilityMappingError):
    """Raised when not exactly one of the three resource-match fields is set."""

    def __str__(self) -> str:
        return 'exactly one of resource_id, resource_kind, resource_path_glob must be set'


class CapabilityMappingUnknownCapabilityIdError(CapabilityMappingError):
    def __init__(self, capability_id: int) -> None:
        self.capability_id = capability_id
        super().__init__(f'Capability {capability_id} not found')


class CapabilityMappingUnknownApplicationIdError(CapabilityMappingError):
    def __init__(self, application_id: UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application {application_id} not found')


class CapabilityMappingUnknownResourceIdError(CapabilityMappingError):
    def __init__(self, resource_id: UUID) -> None:
        self.resource_id = resource_id
        super().__init__(f'Resource {resource_id} not found')


class CapabilityMappingUnknownScopeKeyIdError(CapabilityMappingError):
    def __init__(self, scope_key_id: int) -> None:
        self.scope_key_id = scope_key_id
        super().__init__(f'CapabilityScopeKey {scope_key_id} not found')


class CapabilityMappingDefaultScopeKeyNotSeededError(CapabilityMappingError):
    """Raised when the GLOBAL scope key is missing — Step 2 seed migration hasn't run."""

    def __str__(self) -> str:
        return "Default scope key 'GLOBAL' not found — run alembic upgrade head to apply Step 2 seed"


class CapabilityMappingUnknownActionSlugError(CapabilityMappingError):
    def __init__(self, action_slug: str) -> None:
        self.action_slug = action_slug
        super().__init__(f"Unknown action slug '{action_slug}'")


class CapabilityMappingInUseError(CapabilityMappingError):
    def __init__(self, mapping_id: int, grant_count: int) -> None:
        self.mapping_id = mapping_id
        self.grant_count = grant_count
        super().__init__(f'CapabilityMapping {mapping_id} is referenced by {grant_count} capability grants')
