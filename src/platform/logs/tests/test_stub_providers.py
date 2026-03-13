# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for stub LogSink providers."""

from datetime import datetime

import pytest
from src.platform.logs.factory import UnsupportedProviderError, log_sink_factory
from src.platform.logs.interface import LogSink
from src.platform.logs.providers.elk import ElkLogSink
from src.platform.logs.providers.fluentd import FluentdLogSink
from src.platform.logs.providers.loki import LokiLogSink
from src.platform.logs.providers.nagios import NagiosLogSink
from src.platform.logs.providers.qradar import QradarLogSink
from src.platform.logs.providers.rsyslog import RsyslogLogSink
from src.platform.logs.providers.seq import SeqLogSink
from src.platform.logs.providers.splunk import SplunkLogSink
from src.platform.logs.providers.zabbix import ZabbixLogSink
from src.platform.logs.schemas import LogEvent, LogLevel, LogParticipantKind, new_root_log_event

_STUBS = [
    ElkLogSink,
    LokiLogSink,
    SeqLogSink,
    ZabbixLogSink,
    SplunkLogSink,
    QradarLogSink,
    RsyslogLogSink,
    NagiosLogSink,
    FluentdLogSink,
]

_PROVIDER_NAMES = [
    ('elk', ElkLogSink),
    ('loki', LokiLogSink),
    ('seq', SeqLogSink),
    ('zabbix', ZabbixLogSink),
    ('splunk', SplunkLogSink),
    ('qradar', QradarLogSink),
    ('rsyslog', RsyslogLogSink),
    ('nagios', NagiosLogSink),
    ('fluentd', FluentdLogSink),
]


def _make_event() -> LogEvent:
    return new_root_log_event(
        event_type='test',
        level=LogLevel.INFO,
        message='test',
        component='test',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='platform',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='test',
        target_type=LogParticipantKind.SYSTEM,
        target_id='x',
        payload={},
        timestamp=datetime.now(),
    )


@pytest.mark.parametrize('stub_cls', _STUBS)
def test_stub_is_runtime_checkable_with_log_sink(stub_cls: type[LogSink]) -> None:
    """Each stub is runtime_checkable-compatible with LogSink."""
    sink = stub_cls()
    assert isinstance(sink, LogSink)


@pytest.mark.parametrize('stub_cls', _STUBS)
def test_stub_emit_raises_not_implemented_error(stub_cls: type[LogSink]) -> None:
    """Each stub emit() raises NotImplementedError."""
    sink = stub_cls()
    with pytest.raises(NotImplementedError, match='Stub provider not implemented'):
        sink.emit(_make_event())


@pytest.mark.parametrize('name,cls', _PROVIDER_NAMES)
def test_factory_get_returns_correct_stub(name: str, cls: type[LogSink]) -> None:
    """log_sink_factory.get(provider_name) returns correct stub class."""
    sink = log_sink_factory.get(name)
    assert isinstance(sink, cls)


def test_list_names_includes_all_default_providers() -> None:
    """list_names() includes every default-registered provider."""
    names = log_sink_factory.list_names()
    expected = {
        'file',
        'mq',
        'elk',
        'loki',
        'seq',
        'zabbix',
        'splunk',
        'qradar',
        'rsyslog',
        'nagios',
        'fluentd',
    }
    assert set(names) == expected


def test_get_unknown_raises_unsupported_provider_error() -> None:
    """get('unknown') raises UnsupportedProviderError."""
    with pytest.raises(
        UnsupportedProviderError,
        match=r"Unsupported log sink provider: 'unknown'",
    ):
        log_sink_factory.get('unknown')
