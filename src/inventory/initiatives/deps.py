# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative route dependencies."""

import os

from src.inventory.initiatives.service import InitiativeService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_initiative_service() -> InitiativeService:
    """Return InitiativeService with injected EventService."""
    event_service = EventService(sink=event_sink_factory.get(_get_events_provider()))
    return InitiativeService(event_service=event_service)
