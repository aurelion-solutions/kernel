# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OwnershipAssignment route dependencies."""

from src.inventory.ownership_assignments.service import OwnershipAssignmentService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_ownership_assignment_service() -> OwnershipAssignmentService:
    """Return OwnershipAssignmentService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return OwnershipAssignmentService(log_service=log_service)
