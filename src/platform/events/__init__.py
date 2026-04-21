# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform events slice — domain event bus infrastructure.

Public API
----------
- :class:`EventEnvelope` — immutable domain event envelope
- :class:`EventSink` — transport protocol (runtime-checkable)
- :class:`EventService` — delegates emission to a sink; re-raises on failure
- :class:`NoOpEventService` — silent discard; for fixtures and one-shot boots
- :data:`noop_event_service` — module-level singleton of :class:`NoOpEventService`
- :class:`EventSinkFactory` — resolves sinks by provider name
- :class:`UnsupportedProviderError` — raised by :meth:`EventSinkFactory.get`
- :data:`event_sink_factory` — module-level singleton of :class:`EventSinkFactory`
- :class:`CapturingEventService` — in-memory capture for tests
- :class:`RabbitMQEventSink` — MQ provider (re-exported for DI)
"""

from src.platform.events.factory import EventSinkFactory, UnsupportedProviderError, event_sink_factory
from src.platform.events.interface import EventSink
from src.platform.events.providers.mq import RabbitMQEventSink
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, NoOpEventService, noop_event_service
from src.platform.events.testing import CapturingEventService

__all__ = [
    'CapturingEventService',
    'EventEnvelope',
    'EventParticipantKind',
    'EventService',
    'EventSink',
    'EventSinkFactory',
    'NoOpEventService',
    'RabbitMQEventSink',
    'UnsupportedProviderError',
    'event_sink_factory',
    'noop_event_service',
]
