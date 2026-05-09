# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""TeeEventSink — fan-out to a primary sink and zero or more observability taps."""

from src.platform.events.interface import EventSink
from src.platform.events.schemas import EventEnvelope


class TeeEventSink:
    """Delegates ``emit`` to a primary sink, then to each tap in order.

    The primary sink carries the load-bearing contract (publish to
    ``aurelion.events``).  Errors from the primary propagate to the caller
    unchanged.

    Taps are observability, not load-bearing; silence is intentional.
    Exceptions raised by a tap are swallowed so that a failing tap never
    affects the primary emission.
    """

    def __init__(self, primary: EventSink, *taps: EventSink) -> None:
        self._primary = primary
        self._taps = taps

    async def emit(self, event: EventEnvelope) -> None:
        """Emit to primary (re-raises on error), then to each tap (swallows errors)."""
        await self._primary.emit(event)
        for tap in self._taps:
            try:
                await tap.emit(event)
            except Exception:  # noqa: BLE001 # allowed-broad: event handler swallow
                pass  # tap-sinks are observability, not load-bearing; silence is intentional
