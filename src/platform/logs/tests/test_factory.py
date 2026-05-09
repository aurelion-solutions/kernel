# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LogSinkFactory."""

from datetime import datetime
from pathlib import Path

import pytest
from src.platform.logs.factory import (
    LogSinkFactory,
    UnsupportedProviderError,
    log_sink_factory,
)
from src.platform.logs.interface import LogSink
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.schemas import (
    LogEvent,
    LogLevel,
    LogParticipantKind,
    new_root_log_event,
)


def test_get_file_returns_log_sink_instance(tmp_path: Path) -> None:
    """get('file') returns a LogSink instance."""
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=tmp_path / 'test.jsonl'))
    sink = factory.get('file')
    assert isinstance(sink, LogSink)


async def test_returned_sink_is_usable(tmp_path: Path) -> None:
    """returned sink is usable (emit succeeds)."""
    factory = LogSinkFactory()
    log_path = tmp_path / 'test.jsonl'
    factory.register('file', lambda: FileLogSink(path=log_path))
    sink = factory.get('file')
    event = new_root_log_event(
        level=LogLevel.INFO,
        message='Hello',
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
    await sink.emit(event)
    assert log_path.exists()
    assert 'Hello' in log_path.read_text()


def test_get_unknown_raises_unsupported_provider_error() -> None:
    """get('unknown') raises UnsupportedProviderError."""
    factory = LogSinkFactory()
    with pytest.raises(
        UnsupportedProviderError,
        match=r"Unsupported log sink provider: 'unknown'",
    ):
        factory.get('unknown')


def test_list_names_includes_file() -> None:
    """list_names() includes 'file'."""
    factory = LogSinkFactory()
    names = factory.list_names()
    assert 'file' in names


async def test_custom_provider_can_be_registered_and_resolved(tmp_path: Path) -> None:
    """custom provider can be registered and resolved."""

    class CustomSink(LogSink):
        async def emit(self, event: LogEvent) -> None:
            pass

    factory = LogSinkFactory()
    factory.register('custom', lambda: CustomSink())
    sink = factory.get('custom')
    assert isinstance(sink, CustomSink)


def test_multiple_get_file_calls_return_independent_instances(
    tmp_path: Path,
) -> None:
    """multiple get('file') calls return independent instances."""
    factory = LogSinkFactory()
    log_path = tmp_path / 'test.jsonl'
    factory.register('file', lambda: FileLogSink(path=log_path))
    a = factory.get('file')
    b = factory.get('file')
    assert a is not b


def test_module_singleton_has_file_registered() -> None:
    """log_sink_factory singleton has file registered."""
    sink = log_sink_factory.get('file')
    assert isinstance(sink, FileLogSink)
