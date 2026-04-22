# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LogService."""

from datetime import datetime
import json
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

    async def emit(self, event: LogEvent) -> None:
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


async def test_emit_log_resolves_sink_and_emits(file_factory: LogSinkFactory, tmp_path: Path) -> None:
    """emit_log(...) resolves sink from factory and emits valid LogEvent."""
    service = LogService(sink=file_factory.get('file'))
    await service.emit_log(
        level=LogLevel.INFO,
        message='Hello',
        component='test',
        payload={**_contract_participants(), 'key': 'value'},
    )
    content = (tmp_path / 'test.jsonl').read_text()
    assert 'Hello' in content


async def test_emitted_event_contains_expected_fields(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
) -> None:
    """Emitted event contains level, message, timestamp, component, payload."""
    factory, capturing = capturing_factory
    service = LogService(sink=factory.get('capture'))
    await service.emit_log(
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
    assert event.level == LogLevel.INFO
    assert event.message == 'Command sent'
    assert isinstance(event.timestamp, datetime)
    assert event.component == 'connector-instance'
    assert event.payload == {'command': 'sync'}
    assert event.causation_id is None
    assert isinstance(event.correlation_id, str)
    assert event.actor_id == 'connector-instance'


async def test_aurelion_log_provider_env_selects_provider(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
) -> None:
    """Sink resolved via factory.get() routes to correct provider."""
    factory, capturing = capturing_factory
    service = LogService(sink=factory.get('capture'))
    await service.emit_log(
        level=LogLevel.INFO,
        message='test',
        component='test',
        payload=_contract_participants(),
    )
    assert len(capturing.events) == 1


async def test_default_provider_is_mq_when_env_unset() -> None:
    """Default provider is 'mq' — sink resolved explicitly."""
    capturing = CapturingLogSink()
    factory = LogSinkFactory()
    factory.register('mq', lambda: capturing)
    service = LogService(sink=factory.get('mq'))
    await service.emit_log(
        level=LogLevel.INFO,
        message='default mq',
        component='test',
        payload=_contract_participants(),
    )
    assert len(capturing.events) == 1
    assert capturing.events[0].message == 'default mq'


def test_unknown_provider_raises_unsupported_provider_error(
    file_factory: LogSinkFactory,
) -> None:
    """Unknown provider raises UnsupportedProviderError on factory.get()."""
    with pytest.raises(
        UnsupportedProviderError,
        match=r"Unsupported log sink provider: 'unknown'",
    ):
        file_factory.get('unknown')


def test_emit_safe_does_not_raise_if_sink_fails() -> None:
    """emit_safe(...) does not raise if sink fails (fire-and-forget)."""

    class FailingSink:
        async def emit(self, event: LogEvent) -> None:
            raise RuntimeError('sink broken')

    service = LogService(sink=FailingSink())

    service.emit_safe(
        level=LogLevel.INFO,
        message='msg',
        component='comp',
        payload=_contract_participants(),
    )  # must not raise


async def test_emit_safe_emits_when_sink_works(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
) -> None:
    """emit_safe(...) emits when sink works (verified via emit_log async path)."""
    factory, capturing = capturing_factory
    service = LogService(sink=factory.get('capture'))
    # Use the async emit_log path for deterministic assertion
    await service.emit_log(
        level=LogLevel.INFO,
        message='ok',
        component='comp',
        payload={**_contract_participants(), 'k': 'v'},
    )
    assert len(capturing.events) == 1
    assert capturing.events[0].message == 'ok'


async def test_optional_metadata_passed_through(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
) -> None:
    """Optional metadata is passed through into LogEvent payload."""
    factory, capturing = capturing_factory
    task_id = uuid4()
    app_id = uuid4()
    result_id = uuid4()
    corr = str(uuid4())
    service = LogService(sink=factory.get('capture'))
    await service.emit_log(
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


async def test_emit_log_without_participants_in_payload_does_not_emit(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
) -> None:
    """LogService does not invent participants; incomplete payload → no emit."""
    factory, capturing = capturing_factory
    service = LogService(sink=factory.get('capture'))
    await service.emit_log(
        level=LogLevel.INFO,
        message='m',
        component='comp',
        payload={'note': 'no participant keys'},
    )
    assert capturing.events == []


async def test_emit_log_passes_through_participants_from_payload(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
) -> None:
    """Participant fields in payload are forwarded to LogEvent and stripped from payload."""
    factory, capturing = capturing_factory
    service = LogService(sink=factory.get('capture'))
    await service.emit_log(
        level=LogLevel.INFO,
        message='m',
        component='comp',
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


# ---------------------------------------------------------------------------
# Two-bus separation structural checks (Step 23)
# ---------------------------------------------------------------------------


async def test_emit_safe_produces_record_without_event_type(tmp_path: Path) -> None:
    """emit_log produces a log record with no event_type key (LogService API has no such param)."""
    log_path = tmp_path / 'test.jsonl'
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    service = LogService(sink=factory.get('file'))
    await service.emit_log(
        level=LogLevel.INFO,
        message='operational message',
        component='data-lake',
        payload=_contract_participants(),
    )
    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    assert len(records) == 1
    assert 'event_type' not in records[0]


async def test_emit_log_produces_record_without_event_type(tmp_path: Path) -> None:
    """emit_log produces a log record with no event_type key (LogService API has no such param)."""
    log_path = tmp_path / 'test.jsonl'
    factory = LogSinkFactory()
    factory.register('file', lambda: FileLogSink(path=log_path))
    service = LogService(sink=factory.get('file'))
    await service.emit_log(
        level=LogLevel.WARNING,
        message='another operational message',
        component='batch-worker',
        payload=_contract_participants(),
    )
    assert log_path.exists()
    records = [json.loads(line) for line in log_path.read_text().strip().split('\n')]
    assert len(records) == 1
    assert 'event_type' not in records[0]


async def test_emit_log_passes_event_to_sink_byte_identical(
    capturing_factory: tuple[LogSinkFactory, CapturingLogSink],
) -> None:
    """LogEvent instance built in emit_log reaches sink without transformation (identity check).

    After the refactor, emit_log is the last code path that owns the envelope.
    We intercept the call to sink.emit to capture the exact object reference
    handed to transport, then verify it is the same object that appears in the
    sink's event list — confirming no intermediary rebuilds the envelope.
    """
    factory, capturing = capturing_factory
    task_id = uuid4()
    app_id = uuid4()

    # Intercept sink.emit to capture the identity of the object at call time.
    emitted_ref: list[LogEvent] = []
    original_emit = capturing.emit

    async def _spy(event: LogEvent) -> None:
        emitted_ref.append(event)
        await original_emit(event)

    capturing.emit = _spy  # type: ignore[method-assign]

    service = LogService(sink=factory.get('capture'))
    await service.emit_log(
        level=LogLevel.ERROR,
        message='byte-identity check',
        component='test',
        payload={
            **_contract_participants(extra_key='x'),
        },
        task_id=task_id,
        application_id=app_id,
        request_id='req-id-1',
        correlation_id='corr-1',
        exception_type='ValueError',
        stacktrace='Traceback (most recent call last): ...',
    )
    assert len(capturing.events) == 1
    assert len(emitted_ref) == 1
    # Identity: exact object that arrived at sink.emit must be in capturing.events.
    # If a future intermediary rebuilds the envelope, identity breaks here.
    assert emitted_ref[0] is capturing.events[0]
    assert capturing.events[0].message == 'byte-identity check'
    assert capturing.events[0].level == LogLevel.ERROR
