# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Base class for stub LogSink providers."""

from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent


class StubLogSinkBase(LogSink):
    """Base for stub providers. emit() raises NotImplementedError."""

    async def emit(self, event: LogEvent) -> None:
        raise NotImplementedError('Stub provider not implemented')
