# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LogSink factory for provider resolution by name."""

from collections.abc import Callable

from src.platform.logs.interface import LogSink
from src.platform.logs.providers.elk import ElkLogSink
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.providers.fluentd import FluentdLogSink
from src.platform.logs.providers.loki import LokiLogSink
from src.platform.logs.providers.mq import RabbitMQLogSink
from src.platform.logs.providers.nagios import NagiosLogSink
from src.platform.logs.providers.qradar import QradarLogSink
from src.platform.logs.providers.rsyslog import RsyslogLogSink
from src.platform.logs.providers.seq import SeqLogSink
from src.platform.logs.providers.splunk import SplunkLogSink
from src.platform.logs.providers.zabbix import ZabbixLogSink


class UnsupportedProviderError(Exception):
    """Raised when the requested log sink provider is not registered."""


class LogSinkFactory:
    """Resolves LogSink by provider name. Uses lazy instantiation."""

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], LogSink]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('mq', lambda: RabbitMQLogSink())
        self.register('file', lambda: FileLogSink())
        self.register('elk', lambda: ElkLogSink())
        self.register('loki', lambda: LokiLogSink())
        self.register('seq', lambda: SeqLogSink())
        self.register('zabbix', lambda: ZabbixLogSink())
        self.register('splunk', lambda: SplunkLogSink())
        self.register('qradar', lambda: QradarLogSink())
        self.register('rsyslog', lambda: RsyslogLogSink())
        self.register('nagios', lambda: NagiosLogSink())
        self.register('fluentd', lambda: FluentdLogSink())

    def register(
        self,
        name: str,
        provider_factory: Callable[[], LogSink],
    ) -> None:
        """Register a provider factory. Called for each get()."""
        self._providers[name] = provider_factory

    def list_names(self) -> list[str]:
        """Return list of registered provider names."""
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> LogSink:
        """Return a new LogSink instance for the given provider."""
        if provider_name not in self._providers:
            raise UnsupportedProviderError(f'Unsupported log sink provider: {provider_name!r}')
        return self._providers[provider_name]()


log_sink_factory = LogSinkFactory()
