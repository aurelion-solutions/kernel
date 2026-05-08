# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Domain exceptions for the CapabilityGrant slice."""

from __future__ import annotations

from uuid import UUID


class CapabilityGrantError(Exception):
    """Base exception for the CapabilityGrant slice."""


class CapabilityGrantNotFoundError(CapabilityGrantError):
    """Raised when a CapabilityGrant row cannot be found by id."""

    def __init__(self, grant_id: int) -> None:
        self.grant_id = grant_id
        super().__init__(f'CapabilityGrant {grant_id} not found')


class EffectiveGrantNotFoundForProjectionError(CapabilityGrantError):
    """Raised when project_for_effective_grant is called with an unknown EffectiveGrant id."""

    def __init__(self, effective_grant_id: UUID) -> None:
        self.effective_grant_id = effective_grant_id
        super().__init__(f'EffectiveGrant {effective_grant_id} not found for projection')
