# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative route dependencies."""

from src.inventory.initiatives.service import InitiativeService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_initiative_service() -> InitiativeService:
    """Return InitiativeService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return InitiativeService(log_service=log_service)
