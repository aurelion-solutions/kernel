# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LogReader factory for provider resolution by name."""

from collections.abc import Callable

from src.platform.logs.interface import LogReader
from src.platform.logs.providers._stub_read import StubLogReader
from src.platform.logs.providers.file import FileLogReader


class UnsupportedReadProviderError(Exception):
    """Raised when the requested log read provider is not registered."""


def _stub_reader(provider_name: str) -> Callable[[], LogReader]:
    def factory() -> LogReader:
        return StubLogReader(provider_name)

    return factory


class LogReadFactory:
    """Resolves LogReader by provider name. Uses lazy instantiation."""

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], LogReader]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('file', lambda: FileLogReader())
        for name in ('elk', 'loki', 'seq', 'zabbix', 'splunk', 'qradar', 'rsyslog', 'nagios', 'fluentd'):
            self.register(name, _stub_reader(name))

    def register(
        self,
        name: str,
        provider_factory: Callable[[], LogReader],
    ) -> None:
        """Register a read provider factory."""
        self._providers[name] = provider_factory

    def list_names(self) -> list[str]:
        """Return list of registered provider names."""
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> LogReader:
        """Return a new LogReader instance for the given provider."""
        if provider_name not in self._providers:
            raise UnsupportedReadProviderError(f'Unsupported log read provider: {provider_name!r}')
        return self._providers[provider_name]()


log_read_factory = LogReadFactory()
