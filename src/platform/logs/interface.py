# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LogSink and LogReader interfaces for log abstraction."""

from typing import Protocol, runtime_checkable

from src.platform.logs.schemas import LogEvent


@runtime_checkable
class LogSink(Protocol):
    """Minimal contract for log sinks. All providers must implement this."""

    def emit(self, event: LogEvent) -> None:
        """Emit a log event to the backend."""
        ...


@runtime_checkable
class LogReader(Protocol):
    """Minimal contract for reading log records. Provider-specific implementations."""

    def read(self, limit: int = 100) -> list[dict]:
        """Read up to limit recent log records as JSON-serializable dicts."""
        ...
