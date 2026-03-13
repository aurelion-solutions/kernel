# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LogReadFactory."""

import pytest
from src.platform.logs.providers._stub_read import StubLogReader
from src.platform.logs.providers.file import FileLogReader
from src.platform.logs.read_factory import UnsupportedReadProviderError, log_read_factory


def test_get_file_returns_file_log_reader() -> None:
    """get('file') returns FileLogReader instance."""
    reader = log_read_factory.get('file')
    assert isinstance(reader, FileLogReader)


def test_get_unknown_raises_error() -> None:
    """get(unknown) raises UnsupportedReadProviderError."""
    with pytest.raises(UnsupportedReadProviderError, match='Unsupported log read provider'):
        log_read_factory.get('unknown_provider')


def test_list_names_includes_file_and_stubs() -> None:
    """list_names includes file and all stub providers."""
    names = log_read_factory.list_names()
    assert 'file' in names
    for name in ('elk', 'loki', 'seq', 'zabbix', 'splunk', 'qradar', 'rsyslog', 'nagios', 'fluentd'):
        assert name in names


def test_stub_readers_raise_not_implemented() -> None:
    """Stub providers raise NotImplementedError on read()."""
    for name in ('elk', 'loki', 'seq'):
        reader = log_read_factory.get(name)
        assert isinstance(reader, StubLogReader)
        with pytest.raises(NotImplementedError, match='not implemented'):
            reader.read(limit=10)
