# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding route dependencies."""

import os

from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_artifact_binding_service() -> ArtifactBindingService:
    """Return ArtifactBindingService with injected EventService."""
    event_sink = event_sink_factory.get(_get_events_provider())
    event_service = EventService(sink=event_sink)
    return ArtifactBindingService(event_service=event_service)
