# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EventService, NoOpEventService, and _NoOpEventSink."""

from src.platform.events.interface import EventSink
from src.platform.events.schemas import EventEnvelope


class _NoOpEventSink:
    """Degenerate sink that silently discards events. Not a real transport."""

    async def emit(self, event: EventEnvelope) -> None:  # noqa: ARG002
        return None


class NoOpEventService:
    """Event service that does nothing. For test fixtures and CLI one-shot boots."""

    def __init__(self) -> None:
        self._sink = _NoOpEventSink()

    async def emit(self, event: EventEnvelope) -> None:
        await self._sink.emit(event)


noop_event_service = NoOpEventService()


class EventService:
    """Delegates domain event emission to the configured :class:`EventSink`.

    Re-raises any exception from the sink — domain events are load-bearing,
    not observability. Callers that want forgiveness must wrap the call themselves.
    """

    def __init__(self, sink: EventSink) -> None:
        self._sink = sink

    async def emit(self, event: EventEnvelope) -> None:
        """Emit a domain event. Re-raises on sink failure."""
        # Single-method service: the thin layer exists to enforce the "only services
        # emit events" invariant. Do not collapse into the sink — the seam is the audit
        # boundary for the two-bus contract.
        await self._sink.emit(event)
