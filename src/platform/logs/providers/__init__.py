# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Log sink provider implementations."""

from src.platform.logs.providers.elk import ElkLogSink
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.providers.fluentd import FluentdLogSink
from src.platform.logs.providers.loki import LokiLogSink
from src.platform.logs.providers.nagios import NagiosLogSink
from src.platform.logs.providers.qradar import QradarLogSink
from src.platform.logs.providers.rsyslog import RsyslogLogSink
from src.platform.logs.providers.seq import SeqLogSink
from src.platform.logs.providers.splunk import SplunkLogSink
from src.platform.logs.providers.zabbix import ZabbixLogSink

__all__ = [
    'ElkLogSink',
    'FileLogSink',
    'FluentdLogSink',
    'LokiLogSink',
    'NagiosLogSink',
    'QradarLogSink',
    'RsyslogLogSink',
    'SeqLogSink',
    'SplunkLogSink',
    'ZabbixLogSink',
]
