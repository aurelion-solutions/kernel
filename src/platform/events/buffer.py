# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""In-memory ring buffer for domain events (observability tap)."""

from collections import deque
import threading

from src.platform.events.schemas import EventEnvelope


class InMemoryEventBuffer:
    """Thread-safe ring buffer storing the last ``maxlen`` emitted envelopes.

    Designed for observability only — events are stored as references; the
    ``EventEnvelope`` model is frozen so mutation after append is impossible.
    """

    def __init__(self, maxlen: int = 500) -> None:
        self._deque: deque[EventEnvelope] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, event: EventEnvelope) -> None:
        """Append an envelope to the buffer (thread-safe)."""
        with self._lock:
            self._deque.append(event)

    def snapshot(self, limit: int) -> list[EventEnvelope]:
        """Return the last ``limit`` events, newest first.

        The snapshot is taken under a lock to avoid partial-iteration races
        between ``append`` calls from pika threads and reads from FastAPI handlers.
        """
        with self._lock:
            items = list(self._deque)
        return list(reversed(items))[:limit]


class InMemoryEventBufferSink:
    """EventSink adapter that appends every emitted envelope to an ``InMemoryEventBuffer``."""

    def __init__(self, buffer: InMemoryEventBuffer) -> None:
        self._buffer = buffer

    async def emit(self, event: EventEnvelope) -> None:
        """Append the envelope to the buffer."""
        self._buffer.append(event)
