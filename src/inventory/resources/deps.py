# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resource route dependencies."""

from src.inventory.resources.service import ResourceService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_resource_service() -> ResourceService:
    """Return ResourceService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return ResourceService(log_service=log_service)
