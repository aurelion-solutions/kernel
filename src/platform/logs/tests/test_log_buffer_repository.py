# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for short-term PostgreSQL log buffer persistence and consumer ack policy."""

import json
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from src.platform.logs.buffer_consumer import (
    apply_buffer_outcome_to_channel,
    commit_buffer_delivery_async,
    run_persist_buffer_message_blocking,
)
from src.platform.logs.buffer_repository import insert_buffered_log_event
from src.platform.logs.consumer import parse_connector_log_payload
from src.platform.logs.models import LogEventBufferRow
from src.platform.logs.schemas import (
    LogLevel,
    LogParticipantKind,
    new_downstream_log_event,
    new_root_log_event,
)


def _uuid_str(u) -> str:
    return str(u)


def _minimal_mq_dict():
    eid = uuid4()
    cid = uuid4()
    return (
        {
            'event_id': _uuid_str(eid),
            'timestamp': '2026-04-05T12:30:00Z',
            'level': 'info',
            'message': 'Sync started',
            'component': 'connector-jira',
            'correlation_id': _uuid_str(cid),
            'causation_id': None,
            'payload': {'k': 1},
            'initiator_type': 'user',
            'initiator_id': 'user-42',
            'actor_type': 'connector',
            'actor_id': 'inst-7',
            'target_type': 'system',
            'target_id': 'jira-cloud',
        },
        eid,
        cid,
    )


@pytest.mark.asyncio
async def test_buffer_persists_root_style_event(session_factory) -> None:
    """Root event: causation_id null; key fields stored on row."""
    raw, eid, cid = _minimal_mq_dict()
    del raw['causation_id']
    event = parse_connector_log_payload(raw)
    assert event is not None

    async with session_factory() as session:
        await insert_buffered_log_event(session, event)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(LogEventBufferRow))
        rows = list(result.scalars().all())

    assert len(rows) == 1
    row = rows[0]
    assert row.event_id == eid
    assert row.level == 'info'
    assert row.message == 'Sync started'
    assert row.component == 'connector-jira'
    assert row.correlation_id == _uuid_str(cid)
    assert row.causation_id is None
    assert row.payload == {'k': 1}
    assert row.initiator_type == 'user'
    assert row.initiator_id == 'user-42'
    assert row.actor_type == 'connector'
    assert row.actor_id == 'inst-7'
    assert row.target_type == 'system'
    assert row.target_id == 'jira-cloud'


@pytest.mark.asyncio
async def test_buffer_persists_downstream_style_event(session_factory) -> None:
    """Downstream: causation_id set to parent event_id."""
    parent = new_root_log_event(
        level=LogLevel.INFO,
        message='p',
        component='c',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='s',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='a',
        target_type=LogParticipantKind.SYSTEM,
        target_id='t',
    )
    child = new_downstream_log_event(
        parent,
        level=LogLevel.DEBUG,
        message='child msg',
        component='child-comp',
        initiator_type=LogParticipantKind.USER,
        initiator_id='u',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn',
        target_type=LogParticipantKind.SYSTEM,
        target_id='tgt',
        payload={'step': 2},
    )

    async with session_factory() as session:
        await insert_buffered_log_event(session, child)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(LogEventBufferRow))
        rows = list(result.scalars().all())

    assert len(rows) == 1
    row = rows[0]
    assert row.event_id == child.event_id
    assert row.correlation_id == parent.correlation_id
    assert row.causation_id == parent.event_id
    assert row.payload == {'step': 2}


@pytest.mark.asyncio
async def test_commit_delivery_bad_message_no_row(session_factory) -> None:
    out = await commit_buffer_delivery_async(session_factory, b'not-json')
    assert out == 'bad_message'

    async with session_factory() as session:
        result = await session.execute(select(LogEventBufferRow))
        assert list(result.scalars().all()) == []


@pytest.mark.asyncio
async def test_commit_delivery_failed_on_flush_error(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(session, event) -> None:
        raise OSError('flush failed')

    monkeypatch.setattr(
        'src.platform.logs.buffer_consumer.insert_buffered_log_event',
        boom,
    )
    raw, _, _ = _minimal_mq_dict()
    body = json.dumps(raw).encode('utf-8')
    out = await commit_buffer_delivery_async(session_factory, body)
    assert out == 'commit_failed'


@pytest.mark.asyncio
async def test_commit_delivery_persisted_writes_row(session_factory) -> None:
    raw, _, _ = _minimal_mq_dict()
    body = json.dumps(raw).encode('utf-8')
    out = await commit_buffer_delivery_async(session_factory, body)
    assert out == 'persisted'

    async with session_factory() as session:
        result = await session.execute(select(LogEventBufferRow))
        assert len(list(result.scalars().all())) == 1


def test_run_persist_blocking_smoke_sync_entrypoint(session_factory) -> None:
    """Blocking wrapper is used by the runtime; smoke-test outside async loop."""
    raw, _, _ = _minimal_mq_dict()
    body = json.dumps(raw).encode('utf-8')
    out = run_persist_buffer_message_blocking(session_factory, body)
    assert out == 'persisted'


def test_apply_outcome_ack_on_persisted() -> None:
    ch = MagicMock()
    method = MagicMock(delivery_tag=42)
    apply_buffer_outcome_to_channel(ch, method, 'persisted')
    ch.basic_ack.assert_called_once_with(delivery_tag=42)
    ch.basic_nack.assert_not_called()


def test_apply_outcome_nack_no_requeue_on_bad_message() -> None:
    ch = MagicMock()
    method = MagicMock(delivery_tag=7)
    apply_buffer_outcome_to_channel(ch, method, 'bad_message')
    ch.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)
    ch.basic_ack.assert_not_called()


def test_apply_outcome_nack_requeue_on_commit_failed() -> None:
    ch = MagicMock()
    method = MagicMock(delivery_tag=3)
    apply_buffer_outcome_to_channel(ch, method, 'commit_failed')
    ch.basic_nack.assert_called_once_with(delivery_tag=3, requeue=True)
