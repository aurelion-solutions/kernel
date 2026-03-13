# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LogService."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
from src.platform.logs.factory import LogSinkFactory, UnsupportedProviderError
from src.platform.logs.providers.file import FileLogSink
from src.platform.logs.schemas import LogEvent, LogLevel, LogParticipantKind
from src.platform.logs.service import LogService


def _contract_participants(**overrides: str) -> dict[str, str]:
    """Minimal participant keys required for :meth:`LogService.emit_log` to emit."""
    base = {
        'initiator_type': 'user',
        'initiator_id': 'caller',
        'actor_type': 'connector',
        'actor_id': 'actor-1',
        'target_type': 'system',
        'target_id': 'resource',
    }
    base.update(overrides)
    return base


class CapturingLogSink:
    """Sink that stores emitted events for assertions."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    def emit(self, event: LogEvent) -> None:
        self.events.append(event)


@pytest.fixture
def file_factory(tmp_path: Path) -> LogSinkFactory:
    """Factory with file provider at tmp_path."""
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=tmp_path / 'test.jsonl'))
    return factory


@pytest.fixture
def capturing_factory() -> tuple[LogSinkFactory, CapturingLogSink]:
    """Factory with capturing sink for assertions."""
    capturing = CapturingLogSink()
    factory = LogSinkFactory()
    factory.register('capture', lambda: capturing)
    return factory, capturing


def test_emit_log_resolves_sink_and_emits(file_factory: LogSinkFactory, tmp_path: Path) -> None:
    """emit_log(...) resolves sink from factory and emits valid LogEvent."""
    service = LogService(factory=file_factory, provider_name='file')
    service.emit_log(
        event_type='test.emit',
        level=LogLevel.INFO,
        message='Hello',
        component='test',
        payload={**_contract_participants(), 'key': 'value'},
    )
    content = (tmp_path / 'test.jsonl').read_text()
    assert 'Hello' in content
    assert 'test.emit' in content


def test_emitted_event_contains_expected_fields(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emitted event contains event_type, level, message, timestamp, component, payload."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.emit_log(
        event_type='connector.command.published',
        level=LogLevel.INFO,
        message='Command sent',
        component='connector-instance',
        payload={
            **_contract_participants(actor_id='connector-instance'),
            'command': 'sync',
        },
    )
    assert len(capturing.events) == 1
    event = capturing.events[0]
    assert event.event_type == 'connector.command.published'
    assert event.level == LogLevel.INFO
    assert event.message == 'Command sent'
    assert isinstance(event.timestamp, datetime)
    assert event.component == 'connector-instance'
    assert event.payload == {'command': 'sync'}
    assert event.causation_id is None
    assert isinstance(event.correlation_id, str)
    assert event.actor_id == 'connector-instance'


def test_aurelion_log_provider_env_selects_provider(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AURELION_LOG_PROVIDER env selects provider."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.emit_log(
        event_type='test',
        level=LogLevel.INFO,
        message='test',
        component='test',
        payload=_contract_participants(),
    )
    assert len(capturing.events) == 1


def test_default_provider_is_mq_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default provider is 'mq' when AURELION_LOG_PROVIDER is unset."""
    capturing = CapturingLogSink()
    factory = LogSinkFactory()
    factory.register('mq', lambda: capturing)
    monkeypatch.delenv('AURELION_LOG_PROVIDER', raising=False)
    service = LogService(factory=factory)
    service.emit_log(
        event_type='test',
        level=LogLevel.INFO,
        message='default mq',
        component='test',
        payload=_contract_participants(),
    )
    assert len(capturing.events) == 1
    assert capturing.events[0].message == 'default mq'


def test_unknown_provider_raises_unsupported_provider_error(
    file_factory: LogSinkFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown provider from env raises UnsupportedProviderError."""
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'unknown')
    service = LogService(factory=file_factory, provider_name=None)
    with pytest.raises(
        UnsupportedProviderError,
        match=r"Unsupported log sink provider: 'unknown'",
    ):
        service.emit_log(
            event_type='test',
            level=LogLevel.INFO,
            message='test',
            component='test',
            payload=_contract_participants(),
        )


def test_log_info_emits_with_info_level(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log_info(...) emits with INFO level."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.log_info('test.info', 'message', 'comp', _contract_participants())
    assert capturing.events[0].level == LogLevel.INFO


def test_log_warning_emits_with_warning_level(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log_warning(...) emits with WARNING level."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.log_warning('test.warn', 'message', 'comp', _contract_participants())
    assert capturing.events[0].level == LogLevel.WARNING


def test_log_error_emits_with_error_level(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """log_error(...) emits with ERROR level."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.log_error('test.err', 'message', 'comp', _contract_participants())
    assert capturing.events[0].level == LogLevel.ERROR


def test_emit_safe_does_not_raise_if_sink_fails(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit_safe(...) does not raise if sink fails."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')

    class FailingSink:
        def emit(self, event: LogEvent) -> None:
            raise RuntimeError('sink broken')

    fail_factory = LogSinkFactory()
    fail_factory.register('capture', lambda: FailingSink())
    service = LogService(factory=fail_factory)

    service.emit_safe(
        'test',
        LogLevel.INFO,
        'msg',
        'comp',
        _contract_participants(),
    )  # must not raise


def test_emit_safe_emits_when_sink_works(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """emit_safe(...) emits when sink works."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.emit_safe(
        'safe.test',
        LogLevel.INFO,
        'ok',
        'comp',
        {**_contract_participants(), 'k': 'v'},
    )
    assert len(capturing.events) == 1
    assert capturing.events[0].event_type == 'safe.test'


def test_optional_metadata_passed_through(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional metadata is passed through into LogEvent payload."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    task_id = uuid4()
    app_id = uuid4()
    result_id = uuid4()
    corr = str(uuid4())
    service = LogService(factory=factory)
    service.emit_log(
        event_type='test',
        level=LogLevel.INFO,
        message='test',
        component='test',
        payload=_contract_participants(),
        task_id=task_id,
        application_id=app_id,
        connector_type='mock',
        result_id=result_id,
        request_id='req-123',
        correlation_id=corr,
        exception_type='ValueError',
        stacktrace='Traceback...',
    )
    event = capturing.events[0]
    assert event.correlation_id == corr
    p = event.payload
    assert p['task_id'] == str(task_id)
    assert p['application_id'] == str(app_id)
    assert p['connector_type'] == 'mock'
    assert p['result_id'] == str(result_id)
    assert p['request_id'] == 'req-123'
    assert p['exception_type'] == 'ValueError'
    assert p['stacktrace'] == 'Traceback...'


def test_emit_log_without_participants_in_payload_does_not_emit(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LogService does not invent participants; incomplete payload → no emit."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.emit_log(
        'skipped',
        LogLevel.INFO,
        'm',
        'comp',
        {'note': 'no participant keys'},
    )
    assert capturing.events == []


def test_emit_log_passes_through_participants_from_payload(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Participant fields in payload are forwarded to LogEvent and stripped from payload."""
    factory, capturing = capturing_factory
    monkeypatch.setenv('AURELION_LOG_PROVIDER', 'capture')
    service = LogService(factory=factory)
    service.emit_log(
        'e',
        LogLevel.INFO,
        'm',
        'comp',
        payload={
            'initiator_type': 'connector',
            'initiator_id': 'i99',
            'actor_type': 'user',
            'actor_id': 'a88',
            'target_type': 'system',
            'target_id': 'db',
            'extra': 1,
        },
    )
    event = capturing.events[0]
    assert event.initiator_type == LogParticipantKind.CONNECTOR
    assert event.initiator_id == 'i99'
    assert event.actor_type == LogParticipantKind.USER
    assert event.actor_id == 'a88'
    assert event.target_type == LogParticipantKind.SYSTEM
    assert event.target_id == 'db'
    assert event.payload == {'extra': 1}
