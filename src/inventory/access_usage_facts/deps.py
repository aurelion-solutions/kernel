# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact route dependencies."""

import os

from src.inventory.access_usage_facts.service import AccessUsageFactService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_access_usage_fact_service() -> AccessUsageFactService:
    """Return AccessUsageFactService with injected EventService."""
    event_sink = event_sink_factory.get(_get_events_provider())
    event_service = EventService(sink=event_sink)
    return AccessUsageFactService(event_service=event_service)
