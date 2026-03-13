# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact route dependencies."""

from src.inventory.access_usage_facts.service import AccessUsageFactService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_access_usage_fact_service() -> AccessUsageFactService:
    """Return AccessUsageFactService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return AccessUsageFactService(log_service=log_service)
