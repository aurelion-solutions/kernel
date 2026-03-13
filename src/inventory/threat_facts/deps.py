# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ThreatFact route dependencies."""

from src.inventory.threat_facts.service import ThreatFactService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_threat_fact_service() -> ThreatFactService:
    """Return ThreatFactService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return ThreatFactService(log_service=log_service)
