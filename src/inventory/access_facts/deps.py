# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact route dependencies."""

from src.inventory.access_facts.service import AccessFactService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_access_fact_service() -> AccessFactService:
    """Return AccessFactService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return AccessFactService(log_service=log_service)
