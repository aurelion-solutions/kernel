# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Stub LogReader for providers that do not yet support read."""

from src.platform.logs.interface import LogReader


class StubLogReader(LogReader):
    """Read stub. read() raises NotImplementedError. Use for elk, loki, seq, etc."""

    def __init__(self, provider_name: str) -> None:
        self._provider_name = provider_name

    def read(self, limit: int = 100) -> list[dict]:
        raise NotImplementedError(f'Log read not implemented for provider {self._provider_name!r}')
