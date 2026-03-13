# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""NHI route dependencies."""

from src.inventory.nhi.service import NHIService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_nhi_service() -> NHIService:
    """Return NHIService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return NHIService(log_service=log_service)
