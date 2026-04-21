# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for FileLogSink."""

from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4

import pytest
from src.platform.logs.interface import LogSink
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.schemas import (
    LogEvent,
    LogLevel,
    LogParticipantKind,
    new_root_log_event,
)


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    return tmp_path / 'test.log.jsonl'


@pytest.fixture
def sink(log_path: Path) -> FileLogSink:
    return FileLogSink(path=log_path)


def _minimal(
    *,
    event_type: str = 'test',
    message: str = 'm',
    payload: dict | None = None,
    ts: datetime | None = None,
) -> LogEvent:
    return new_root_log_event(
        event_type=event_type,
        level=LogLevel.INFO,
        message=message,
        component='test',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='platform',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='test',
        target_type=LogParticipantKind.SYSTEM,
        target_id='x',
        payload=payload or {},
        timestamp=ts,
    )


async def test_emit_writes_one_json_line_to_file(sink: FileLogSink, log_path: Path) -> None:
    """emit writes one JSON line to file."""
    event = _minimal(
        event_type='test.emit',
        message='Hello',
        payload={'key': 'value'},
    )
    await sink.emit(event)
    content = log_path.read_text(encoding='utf-8')
    lines = content.strip().split('\n')
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record['event_type'] == 'test.emit'
    assert record['message'] == 'Hello'


async def test_repeated_emit_appends_multiple_lines(sink: FileLogSink, log_path: Path) -> None:
    """repeated emit appends multiple lines."""
    base_ts = datetime.now(UTC)
    for i in range(3):
        event = _minimal(
            event_type='test.append',
            message=f'Line {i}',
            payload={'index': i},
            ts=base_ts,
        )
        await sink.emit(event)
    lines = log_path.read_text(encoding='utf-8').strip().split('\n')
    assert len(lines) == 3
    for i, line in enumerate(lines):
        record = json.loads(line)
        assert record['message'] == f'Line {i}'
        assert record['payload']['index'] == i


def test_file_path_configurable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """file path configurable via env."""
    monkeypatch.setenv('AURELION_LOG_FILE_PATH', '/custom/path/logs.jsonl')
    sink = FileLogSink()
    assert sink._path == Path('/custom/path/logs.jsonl')


async def test_emitted_json_contains_expected_fields(sink: FileLogSink, log_path: Path) -> None:
    """emitted JSON contains expected fields."""
    event = new_root_log_event(
        event_type='connector.command.published',
        level=LogLevel.INFO,
        message='Command sent',
        component='connector-instance',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='platform',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='connector-instance',
        target_type=LogParticipantKind.CONNECTOR,
        target_id='c1',
        payload={'command': 'sync'},
        timestamp=datetime(2025, 3, 22, 12, 0, 0, tzinfo=UTC),
    )
    await sink.emit(event)
    record = json.loads(log_path.read_text(encoding='utf-8').strip())
    assert record['event_type'] == 'connector.command.published'
    assert record['level'] == 'info'
    assert record['message'] == 'Command sent'
    assert 'timestamp' in record
    assert record['component'] == 'connector-instance'
    assert record['payload'] == {'command': 'sync'}
    # causation_id is None for root events; exclude_none=True means the key is absent
    assert record.get('causation_id') is None
    assert 'event_id' in record
    assert 'correlation_id' in record


async def test_parent_directory_created_if_missing(tmp_path: Path) -> None:
    """parent directory created if missing."""
    nested = tmp_path / 'a' / 'b' / 'c' / 'logs.jsonl'
    sink = FileLogSink(path=nested)
    await sink.emit(_minimal())
    assert nested.exists()
    assert nested.parent.exists()


async def test_payload_extras_serialized(sink: FileLogSink, log_path: Path) -> None:
    """Arbitrary payload keys serialize with the event."""
    task_id = uuid4()
    app_id = uuid4()
    result_id = uuid4()
    event = new_root_log_event(
        event_type='test.optional',
        level=LogLevel.WARNING,
        message='With payload extras',
        component='test',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='platform',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='test',
        target_type=LogParticipantKind.SYSTEM,
        target_id='x',
        payload={
            'task_id': str(task_id),
            'application_id': str(app_id),
            'connector_type': 'mock',
            'result_id': str(result_id),
            'request_id': 'req-123',
            'client_ref': 'corr-456',
            'exception_type': 'ValueError',
            'stacktrace': 'Traceback...',
        },
    )
    await sink.emit(event)
    record = json.loads(log_path.read_text(encoding='utf-8').strip())
    p = record['payload']
    assert p['task_id'] == str(task_id)
    assert p['application_id'] == str(app_id)
    assert p['connector_type'] == 'mock'
    assert p['result_id'] == str(result_id)
    assert p['request_id'] == 'req-123'
    assert p['client_ref'] == 'corr-456'
    assert p['exception_type'] == 'ValueError'
    assert p['stacktrace'] == 'Traceback...'


def test_file_log_sink_satisfies_protocol() -> None:
    """FileLogSink satisfies LogSink protocol."""
    sink = FileLogSink(path=Path('/tmp/test.jsonl'))
    assert isinstance(sink, LogSink)
