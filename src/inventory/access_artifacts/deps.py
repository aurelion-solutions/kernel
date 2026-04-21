# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact route dependencies."""

import os

from src.inventory.access_artifacts.service import AccessArtifactService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_access_artifact_service() -> AccessArtifactService:
    """Return AccessArtifactService with injected EventService."""
    event_sink = event_sink_factory.get(_get_events_provider())
    event_service = EventService(sink=event_sink)
    return AccessArtifactService(event_service=event_service)
