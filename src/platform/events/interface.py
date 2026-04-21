# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EventSink protocol for domain event transport abstraction."""

from typing import Protocol, runtime_checkable

from src.platform.events.schemas import EventEnvelope


@runtime_checkable
class EventSink(Protocol):
    """Minimal contract for event sinks. All providers must implement this."""

    async def emit(self, event: EventEnvelope) -> None:
        """Publish a domain event to the backend transport."""
        ...
