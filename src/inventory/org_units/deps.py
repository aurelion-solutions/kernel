# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit route dependencies."""

import os

from src.inventory.org_units.service import OrgUnitService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_org_unit_service() -> OrgUnitService:
    """Return OrgUnitService with injected EventService."""
    event_sink = event_sink_factory.get(_get_events_provider())
    event_service = EventService(sink=event_sink)
    return OrgUnitService(event_service=event_service)
