# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resource route dependencies."""

import os

from src.inventory.resources.service import ResourceService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_resource_service() -> ResourceService:
    """Return ResourceService with injected EventService."""
    event_service = EventService(sink=event_sink_factory.get(_get_events_provider()))
    return ResourceService(event_service=event_service)
