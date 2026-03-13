# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for MQ log consumer payload normalization and parsing."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from src.platform.logs.consumer import normalize_mq_log_event_payload, parse_connector_log_payload
from src.platform.logs.schemas import LogLevel, LogParticipantKind


def _uuid_str(u: UUID) -> str:
    return str(u)


def _minimal_contract_dict(
    *,
    event_id: UUID | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
    payload: dict | None = None,
) -> dict:
    eid = event_id or uuid4()
    cid = correlation_id or uuid4()
    return {
        'event_id': _uuid_str(eid),
        'event_type': 'connector.sync.started',
        'timestamp': '2026-04-05T12:30:00Z',
        'level': 'info',
        'message': 'Sync started',
        'component': 'connector-jira',
        'correlation_id': _uuid_str(cid),
        'causation_id': None if causation_id is None else _uuid_str(causation_id),
        'payload': {} if payload is None else payload,
        'initiator_type': 'user',
        'initiator_id': 'user-42',
        'actor_type': 'connector',
        'actor_id': 'inst-7',
        'target_type': 'system',
        'target_id': 'jira-cloud',
    }


def test_parse_root_omitted_causation_id_defaults_to_none():
    raw = _minimal_contract_dict()
    del raw['causation_id']
    event = parse_connector_log_payload(raw)
    assert event is not None
    assert event.causation_id is None


def test_parse_valid_root_style_payload():
    """Root-style event: causation_id null; all contract fields parsed."""
    cid = uuid4()
    eid = uuid4()
    raw = _minimal_contract_dict(event_id=eid, correlation_id=cid, causation_id=None)
    event = parse_connector_log_payload(raw)
    assert event is not None
    assert event.event_id == eid
    assert event.correlation_id == _uuid_str(cid)
    assert event.causation_id is None
    assert event.event_type == 'connector.sync.started'
    assert event.level == LogLevel.INFO
    assert event.message == 'Sync started'
    assert event.component == 'connector-jira'
    assert event.payload == {}
    assert event.initiator_type == LogParticipantKind.USER
    assert event.initiator_id == 'user-42'
    assert event.actor_type == LogParticipantKind.CONNECTOR
    assert event.actor_id == 'inst-7'
    assert event.target_type == LogParticipantKind.SYSTEM
    assert event.target_id == 'jira-cloud'
    assert event.timestamp == datetime(2026, 4, 5, 12, 30, 0, tzinfo=UTC)


def test_parse_valid_downstream_style_payload():
    """Downstream: new event_id, same correlation_id, causation_id = parent event_id."""
    corr = uuid4()
    parent_eid = uuid4()
    child_eid = uuid4()
    raw = {
        'event_id': _uuid_str(child_eid),
        'event_type': 'connector.step.done',
        'timestamp': '2026-04-05T12:31:00+00:00',
        'level': 'debug',
        'message': 'Step complete',
        'component': 'connector-jira',
        'correlation_id': _uuid_str(corr),
        'causation_id': _uuid_str(parent_eid),
        'payload': {'step': 2},
        'initiator_type': 'system',
        'initiator_id': 'kernel',
        'actor_type': 'connector',
        'actor_id': 'inst-7',
        'target_type': 'system',
        'target_id': 'queue',
    }
    event = parse_connector_log_payload(raw)
    assert event is not None
    assert event.event_id == child_eid
    assert event.correlation_id == _uuid_str(corr)
    assert event.causation_id == parent_eid
    assert event.payload == {'step': 2}


def test_preserves_correlation_and_causation_ids():
    corr = uuid4()
    cause = uuid4()
    eid = uuid4()
    raw = _minimal_contract_dict(
        event_id=eid,
        correlation_id=corr,
        causation_id=cause,
        payload={'k': 1},
    )
    event = parse_connector_log_payload(raw)
    assert event is not None
    assert event.correlation_id == _uuid_str(corr)
    assert event.causation_id == cause


def test_preserves_initiator_actor_target():
    raw = _minimal_contract_dict()
    raw['initiator_type'] = 'CONNECTOR'
    raw['actor_type'] = 'USER'
    raw['target_type'] = 'SYSTEM'
    raw['initiator_id'] = 'i1'
    raw['actor_id'] = 'a1'
    raw['target_id'] = 't1'
    event = parse_connector_log_payload(raw)
    assert event is not None
    assert event.initiator_type == LogParticipantKind.CONNECTOR
    assert event.actor_type == LogParticipantKind.USER
    assert event.target_type == LogParticipantKind.SYSTEM
    assert event.initiator_id == 'i1'
    assert event.actor_id == 'a1'
    assert event.target_id == 't1'


def test_normalize_uppercase_level():
    raw = _minimal_contract_dict()
    raw['level'] = 'WARNING'
    n = normalize_mq_log_event_payload(raw)
    assert n['level'] == 'warning'


def test_normalize_payload_defaults_to_empty_object():
    raw = _minimal_contract_dict()
    del raw['payload']
    n = normalize_mq_log_event_payload(raw)
    assert n['payload'] == {}


def test_parse_fails_malformed_required_field():
    raw = _minimal_contract_dict()
    raw['message'] = ''
    assert parse_connector_log_payload(raw) is None


def test_parse_fails_missing_required_field():
    raw = _minimal_contract_dict()
    del raw['correlation_id']
    assert parse_connector_log_payload(raw) is None


def test_parse_accepts_non_uuid_correlation_id():
    raw = _minimal_contract_dict()
    raw['correlation_id'] = '  trace-ledger-7  '
    event = parse_connector_log_payload(raw)
    assert event is not None
    assert event.correlation_id == 'trace-ledger-7'


def test_parse_fails_payload_not_object():
    raw = _minimal_contract_dict()
    raw['payload'] = [1, 2]
    assert parse_connector_log_payload(raw) is None


def test_normalize_payload_null_becomes_empty_dict():
    raw = _minimal_contract_dict()
    raw['payload'] = None
    n = normalize_mq_log_event_payload(raw)
    assert n['payload'] == {}
