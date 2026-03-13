# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LogEvent schema, propagation helpers, and LogSink protocol."""

from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
import pytest
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import (
    LogEvent,
    LogLevel,
    LogParticipantKind,
    new_downstream_log_event,
    new_downstream_log_event_from_parent_id,
    new_root_log_event,
)


def _base_kwargs() -> dict:
    return {
        'event_type': 'platform.request.started',
        'level': LogLevel.INFO,
        'message': 'Started',
        'component': 'api',
        'initiator_type': LogParticipantKind.USER,
        'initiator_id': 'user-1',
        'actor_type': LogParticipantKind.SYSTEM,
        'actor_id': 'kernel-api',
        'target_type': LogParticipantKind.CONNECTOR,
        'target_id': 'app-1',
    }


def test_valid_root_event_via_helper():
    """Root helper generates event_id and correlation_id; causation_id is None."""
    root = new_root_log_event(**_base_kwargs())
    assert root.causation_id is None
    assert root.event_id is not None
    assert root.correlation_id is not None
    assert root.event_id != root.correlation_id
    assert root.payload == {}


def test_valid_downstream_event_via_helper():
    """Downstream helper: new event_id, same correlation_id, causation_id = parent.event_id."""
    root = new_root_log_event(**_base_kwargs())
    child = new_downstream_log_event(
        root,
        event_type='connector.command.sent',
        level=LogLevel.DEBUG,
        message='Sent to connector',
        component='connector-bridge',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='kernel-api',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn-inst-1',
        target_type=LogParticipantKind.SYSTEM,
        target_id='jira-cloud',
    )
    assert child.event_id != root.event_id
    assert child.correlation_id == root.correlation_id
    assert child.causation_id == root.event_id


def test_downstream_from_parent_id_matches_downstream_from_parent_event():
    root = new_root_log_event(**_base_kwargs())
    from_parent = new_downstream_log_event_from_parent_id(
        parent_event_id=root.event_id,
        correlation_id=root.correlation_id,
        event_type='child',
        level=LogLevel.INFO,
        message='m',
        component='c',
        initiator_type=LogParticipantKind.USER,
        initiator_id='u',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='a',
        target_type=LogParticipantKind.SYSTEM,
        target_id='t',
    )
    from_obj = new_downstream_log_event(
        root,
        event_type='child',
        level=LogLevel.INFO,
        message='m',
        component='c',
        initiator_type=LogParticipantKind.USER,
        initiator_id='u',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='a',
        target_type=LogParticipantKind.SYSTEM,
        target_id='t',
    )
    assert from_parent.correlation_id == from_obj.correlation_id
    assert from_parent.causation_id == from_obj.causation_id


def test_capability_participant_kind_value():
    assert LogParticipantKind.CAPABILITY.value == 'capability'


def test_trace_root_allows_causation_id_none_downstream_sets_parent_event_id():
    """Schema allows causation_id None only for roots; downstream sets parent id."""
    ts = datetime.now(UTC)
    cid = uuid4()
    e_root = uuid4()
    root = LogEvent(
        event_id=e_root,
        event_type='t',
        timestamp=ts,
        level=LogLevel.INFO,
        message='m',
        component='c',
        correlation_id=cid,
        causation_id=None,
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='sys',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='sys',
        target_type=LogParticipantKind.SYSTEM,
        target_id='x',
    )
    assert root.causation_id is None

    e_child = uuid4()
    child = LogEvent(
        event_id=e_child,
        event_type='t2',
        timestamp=ts,
        level=LogLevel.INFO,
        message='m2',
        component='c2',
        correlation_id=cid,
        causation_id=e_root,
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='sys',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='sys',
        target_type=LogParticipantKind.SYSTEM,
        target_id='x',
    )
    assert child.causation_id == e_root


def test_downstream_helper_always_sets_non_null_causation():
    """Propagation helper ties causation_id to the parent event (never None)."""
    root = new_root_log_event(**_base_kwargs())
    base = _base_kwargs()
    base['event_type'] = 'child.step'
    base['message'] = 'child'
    child = new_downstream_log_event(root, **base)
    assert child.causation_id is not None
    assert child.causation_id == root.event_id


def test_required_fields_validation():
    """Omitting required fields fails validation."""
    ts = datetime.now(UTC)
    cid = uuid4()
    eid = uuid4()
    with pytest.raises(ValidationError):
        LogEvent(
            event_id=eid,
            event_type='t',
            timestamp=ts,
            level=LogLevel.INFO,
            message='',
            component='c',
            correlation_id=cid,
            initiator_type=LogParticipantKind.SYSTEM,
            initiator_id='i',
            actor_type=LogParticipantKind.SYSTEM,
            actor_id='a',
            target_type=LogParticipantKind.SYSTEM,
            target_id='t',
        )


def test_causation_id_must_not_equal_event_id():
    """Downstream causation must reference the parent, not this event's id."""
    ts = datetime.now(UTC)
    cid = uuid4()
    eid = uuid4()
    with pytest.raises(ValidationError):
        LogEvent(
            event_id=eid,
            event_type='t',
            timestamp=ts,
            level=LogLevel.INFO,
            message='m',
            component='c',
            correlation_id=cid,
            causation_id=eid,
            initiator_type=LogParticipantKind.SYSTEM,
            initiator_id='sys',
            actor_type=LogParticipantKind.SYSTEM,
            actor_id='sys',
            target_type=LogParticipantKind.SYSTEM,
            target_id='x',
        )


def test_unknown_fields_rejected():
    """Model is strict for forward-compatible parsing."""
    ts = datetime.now(UTC)
    with pytest.raises(ValidationError):
        LogEvent(
            event_id=uuid4(),
            event_type='t',
            timestamp=ts,
            level=LogLevel.INFO,
            message='m',
            component='c',
            correlation_id=uuid4(),
            initiator_type=LogParticipantKind.SYSTEM,
            initiator_id='s',
            actor_type=LogParticipantKind.SYSTEM,
            actor_id='s',
            target_type=LogParticipantKind.SYSTEM,
            target_id='x',
            extra_field='nope',  # type: ignore[call-arg]
        )


def test_model_dump_json_roundtrip():
    """Serialization preserves propagation fields."""
    root = new_root_log_event(**_base_kwargs(), payload={'k': 1})
    data = root.model_dump(mode='json')
    again = LogEvent.model_validate(data)
    assert again == root


def test_valid_log_level_values_accepted():
    """Valid LogLevel values accepted."""
    for level in (
        LogLevel.DEBUG,
        LogLevel.INFO,
        LogLevel.WARNING,
        LogLevel.ERROR,
        LogLevel.CRITICAL,
    ):
        kw = _base_kwargs()
        kw['level'] = level
        event = new_root_log_event(**kw)
        assert event.level == level


def test_invalid_log_level_rejected():
    """Invalid LogLevel values rejected."""
    with pytest.raises(ValidationError):
        LogEvent(
            event_id=uuid4(),
            event_type='test',
            timestamp=datetime.now(UTC),
            level='invalid',  # type: ignore[arg-type]
            message='test',
            component='test',
            correlation_id=uuid4(),
            initiator_type=LogParticipantKind.SYSTEM,
            initiator_id='s',
            actor_type=LogParticipantKind.SYSTEM,
            actor_id='s',
            target_type=LogParticipantKind.SYSTEM,
            target_id='x',
        )


def test_log_sink_is_runtime_checkable():
    """LogSink is runtime_checkable."""
    assert hasattr(LogSink, '__instancecheck__')


def test_minimal_mock_implementation_satisfies_log_sink():
    """Minimal mock implementation satisfies LogSink via structural subtyping."""

    class MockLogSink:
        def __init__(self) -> None:
            self.emitted: list[LogEvent] = []

        def emit(self, event: LogEvent) -> None:
            self.emitted.append(event)

    mock = MockLogSink()
    assert isinstance(mock, LogSink)
    event = new_root_log_event(**_base_kwargs())
    mock.emit(event)
    assert len(mock.emitted) == 1
    assert mock.emitted[0] == event
