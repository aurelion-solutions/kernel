# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Test support: CapturingLogSink records emitted LogEvents in memory."""

from src.platform.logs.schemas import LogEvent


class CapturingLogSink:
    """In-memory log sink for tests.

    Appends every emitted :class:`LogEvent` to ``self.records``.
    Satisfies the log sink protocol (async ``emit`` method), so it may be
    injected into :class:`~src.platform.logs.service.LogService` via
    :class:`~src.platform.logs.factory.LogSinkFactory`.
    """

    def __init__(self) -> None:
        self.records: list[LogEvent] = []

    async def emit(self, event: LogEvent) -> None:
        """Append the event. Event is frozen — no defensive copy needed."""
        self.records.append(event)

    def clear(self) -> None:
        """Empty the recorded list."""
        self.records.clear()
