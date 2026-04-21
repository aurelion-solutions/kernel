# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Test support: CapturingEventService records emitted envelopes in memory."""

from src.platform.events.schemas import EventEnvelope


class CapturingEventService:
    """In-memory event service for tests.

    Appends every emitted :class:`EventEnvelope` to ``self.emitted``.
    Satisfies the :class:`~src.platform.events.interface.EventSink` protocol,
    so it may be injected into :class:`~src.platform.events.service.EventService`
    as a sink when a test wants to route through the full service path.
    """

    def __init__(self) -> None:
        self.emitted: list[EventEnvelope] = []

    async def emit(self, event: EventEnvelope) -> None:
        """Append the envelope. Envelope is frozen — no defensive copy needed."""
        self.emitted.append(event)

    def filter_by_type(self, event_type: str) -> list[EventEnvelope]:
        """Return a new list of envelopes whose ``event_type`` matches exactly."""
        return [e for e in self.emitted if e.event_type == event_type]

    def clear(self) -> None:
        """Empty the recorded list."""
        self.emitted.clear()
