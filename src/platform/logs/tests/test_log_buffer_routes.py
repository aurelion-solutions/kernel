# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for GET ``/api/v0/log-buffer``."""

from datetime import UTC, datetime, timedelta

import pytest
from src.platform.logs.buffer_repository import insert_buffered_log_event
from src.platform.logs.schemas import LogEvent, LogLevel, LogParticipantKind, new_root_log_event


def _root(
    *,
    correlation_id: str,
    target_type: LogParticipantKind = LogParticipantKind.SYSTEM,
    target_id: str = 'default-target',
    message: str = 'm',
    timestamp: datetime | None = None,
    level: LogLevel = LogLevel.INFO,
    initiator_type: LogParticipantKind = LogParticipantKind.SYSTEM,
    initiator_id: str = 'i',
    actor_type: LogParticipantKind = LogParticipantKind.SYSTEM,
    actor_id: str = 'a',
) -> LogEvent:
    return new_root_log_event(
        level=level,
        message=message,
        component='test',
        initiator_type=initiator_type,
        initiator_id=initiator_id,
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        correlation_id=correlation_id,
        timestamp=timestamp,
    )


@pytest.mark.asyncio
async def test_log_buffer_no_filter_returns_400(client) -> None:
    r = await client.get('/api/v0/log-buffer')
    assert r.status_code == 400
    assert 'filter' in r.json()['detail'].lower()


@pytest.mark.asyncio
async def test_log_buffer_target_id_without_type_returns_400(client) -> None:
    r = await client.get('/api/v0/log-buffer', params={'target_id': 'x'})
    assert r.status_code == 400
    assert 'together' in r.json()['detail'].lower()


@pytest.mark.asyncio
async def test_log_buffer_target_type_without_id_returns_400(client) -> None:
    r = await client.get('/api/v0/log-buffer', params={'target_type': 'system'})
    assert r.status_code == 400
    assert 'together' in r.json()['detail'].lower()


@pytest.mark.asyncio
async def test_log_buffer_initiator_id_without_type_returns_400(client) -> None:
    r = await client.get('/api/v0/log-buffer', params={'initiator_id': 'u1'})
    assert r.status_code == 400
    assert 'initiator' in r.json()['detail'].lower()


@pytest.mark.asyncio
async def test_log_buffer_initiator_type_without_id_returns_400(client) -> None:
    r = await client.get('/api/v0/log-buffer', params={'initiator_type': 'user'})
    assert r.status_code == 400
    assert 'initiator' in r.json()['detail'].lower()


@pytest.mark.asyncio
async def test_log_buffer_actor_id_without_type_returns_400(client) -> None:
    r = await client.get('/api/v0/log-buffer', params={'actor_id': 'a1'})
    assert r.status_code == 400
    assert 'actor' in r.json()['detail'].lower()


@pytest.mark.asyncio
async def test_log_buffer_actor_type_without_id_returns_400(client) -> None:
    r = await client.get('/api/v0/log-buffer', params={'actor_type': 'connector'})
    assert r.status_code == 400
    assert 'actor' in r.json()['detail'].lower()


@pytest.mark.asyncio
async def test_log_buffer_invalid_order_returns_422(client) -> None:
    r = await client.get(
        '/api/v0/log-buffer',
        params={'correlation_id': 'c', 'order': 'newest'},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_log_buffer_limit_out_of_range_returns_422(client) -> None:
    r = await client.get(
        '/api/v0/log-buffer',
        params={'correlation_id': 'c', 'limit': 0},
    )
    assert r.status_code == 422
    r2 = await client.get(
        '/api/v0/log-buffer',
        params={'correlation_id': 'c', 'limit': 1001},
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_log_buffer_query_by_correlation_id(client, session_factory) -> None:
    a = _root(correlation_id='trace-a', message='a')
    b = _root(correlation_id='trace-b', message='b')
    async with session_factory() as session:
        await insert_buffered_log_event(session, a)
        await insert_buffered_log_event(session, b)
        await session.commit()

    r = await client.get('/api/v0/log-buffer', params={'correlation_id': 'trace-a'})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'a'
    assert data[0]['correlation_id'] == 'trace-a'


@pytest.mark.asyncio
async def test_log_buffer_query_by_target_type_and_id(client, session_factory) -> None:
    e1 = _root(correlation_id='t1', target_id='resource-1', message='one')
    e2 = _root(correlation_id='t2', target_id='resource-2', message='two')
    async with session_factory() as session:
        await insert_buffered_log_event(session, e1)
        await insert_buffered_log_event(session, e2)
        await session.commit()

    r = await client.get(
        '/api/v0/log-buffer',
        params={'target_type': 'system', 'target_id': 'resource-1'},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'one'


@pytest.mark.asyncio
async def test_log_buffer_combined_correlation_and_target(client, session_factory) -> None:
    same_cid = 'shared-trace'
    a = _root(correlation_id=same_cid, target_id='keep-me', message='hit')
    b = _root(correlation_id=same_cid, target_id='other', message='miss')
    async with session_factory() as session:
        await insert_buffered_log_event(session, a)
        await insert_buffered_log_event(session, b)
        await session.commit()

    r = await client.get(
        '/api/v0/log-buffer',
        params={
            'correlation_id': same_cid,
            'target_type': 'system',
            'target_id': 'keep-me',
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'hit'


@pytest.mark.asyncio
async def test_log_buffer_ordered_by_timestamp_desc(client, session_factory) -> None:
    base = datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC)
    e_old = _root(correlation_id='ord', message='old', timestamp=base)
    e_mid = _root(correlation_id='ord', message='mid', timestamp=base + timedelta(minutes=1))
    e_new = _root(correlation_id='ord', message='new', timestamp=base + timedelta(minutes=2))
    async with session_factory() as session:
        await insert_buffered_log_event(session, e_old)
        await insert_buffered_log_event(session, e_mid)
        await insert_buffered_log_event(session, e_new)
        await session.commit()

    r = await client.get('/api/v0/log-buffer', params={'correlation_id': 'ord', 'order': 'desc'})
    assert r.status_code == 200
    messages = [row['message'] for row in r.json()]
    assert messages == ['new', 'mid', 'old']


@pytest.mark.asyncio
async def test_log_buffer_ordered_by_timestamp_asc_trace_chain(client, session_factory) -> None:
    base = datetime(2026, 3, 10, 10, 0, 0, tzinfo=UTC)
    cid = 'chain-asc'
    e_old = _root(correlation_id=cid, message='old', timestamp=base)
    e_mid = _root(correlation_id=cid, message='mid', timestamp=base + timedelta(minutes=1))
    e_new = _root(correlation_id=cid, message='new', timestamp=base + timedelta(minutes=2))
    async with session_factory() as session:
        await insert_buffered_log_event(session, e_old)
        await insert_buffered_log_event(session, e_mid)
        await insert_buffered_log_event(session, e_new)
        await session.commit()

    r = await client.get('/api/v0/log-buffer', params={'correlation_id': cid, 'order': 'asc'})
    assert r.status_code == 200
    messages = [row['message'] for row in r.json()]
    assert messages == ['old', 'mid', 'new']


@pytest.mark.asyncio
async def test_log_buffer_limit_caps_rows(client, session_factory) -> None:
    base = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    events = [_root(correlation_id='lim', message=f'm{i}', timestamp=base + timedelta(seconds=i)) for i in range(5)]
    async with session_factory() as session:
        for e in events:
            await insert_buffered_log_event(session, e)
        await session.commit()

    r = await client.get(
        '/api/v0/log-buffer',
        params={'correlation_id': 'lim', 'limit': 2},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert data[0]['message'] == 'm4'
    assert data[1]['message'] == 'm3'


@pytest.mark.asyncio
async def test_log_buffer_filter_by_initiator_pair(client, session_factory) -> None:
    e_hit = _root(
        correlation_id='c1',
        initiator_type=LogParticipantKind.USER,
        initiator_id='alice',
        message='hit',
    )
    e_miss = _root(correlation_id='c2', initiator_id='bob', message='miss')
    async with session_factory() as session:
        await insert_buffered_log_event(session, e_hit)
        await insert_buffered_log_event(session, e_miss)
        await session.commit()

    r = await client.get(
        '/api/v0/log-buffer',
        params={'initiator_type': 'user', 'initiator_id': 'alice'},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'hit'


@pytest.mark.asyncio
async def test_log_buffer_filter_by_actor_pair(client, session_factory) -> None:
    e_hit = _root(
        correlation_id='c1',
        actor_type=LogParticipantKind.CONNECTOR,
        actor_id='conn-99',
        message='hit',
    )
    e_miss = _root(correlation_id='c2', actor_id='other', message='miss')
    async with session_factory() as session:
        await insert_buffered_log_event(session, e_hit)
        await insert_buffered_log_event(session, e_miss)
        await session.commit()

    r = await client.get(
        '/api/v0/log-buffer',
        params={'actor_type': 'connector', 'actor_id': 'conn-99'},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'hit'


@pytest.mark.asyncio
async def test_log_buffer_filter_by_level(client, session_factory) -> None:
    e1 = _root(correlation_id='l1', level=LogLevel.INFO, message='i')
    e2 = _root(correlation_id='l2', level=LogLevel.ERROR, message='e')
    async with session_factory() as session:
        await insert_buffered_log_event(session, e1)
        await insert_buffered_log_event(session, e2)
        await session.commit()

    r = await client.get('/api/v0/log-buffer', params={'level': 'error'})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'e'


@pytest.mark.asyncio
async def test_log_buffer_filter_from_ts_to_ts(client, session_factory) -> None:
    base = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    e_in = _root(correlation_id='tsf', message='in', timestamp=base + timedelta(minutes=30))
    e_early = _root(correlation_id='tsf', message='early', timestamp=base)
    e_late = _root(correlation_id='tsf', message='late', timestamp=base + timedelta(hours=2))
    async with session_factory() as session:
        for e in (e_early, e_in, e_late):
            await insert_buffered_log_event(session, e)
        await session.commit()

    r = await client.get(
        '/api/v0/log-buffer',
        params={
            'correlation_id': 'tsf',
            'from_ts': (base + timedelta(minutes=15)).isoformat(),
            'to_ts': (base + timedelta(hours=1)).isoformat(),
            'order': 'asc',
        },
    )
    assert r.status_code == 200
    messages = [row['message'] for row in r.json()]
    assert messages == ['in']


@pytest.mark.asyncio
async def test_log_buffer_filter_by_payload_step_run_id(client, session_factory) -> None:
    """Per-step UI scopes logs by ``payload->>'step_run_id'`` — the side-channel
    attribute stamped by the runner and the step-scoped log façade."""
    e_match = new_root_log_event(
        level=LogLevel.INFO,
        message='step-tagged',
        component='test',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='i',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='a',
        target_type=LogParticipantKind.SYSTEM,
        target_id='default',
        correlation_id='step-payload-test',
        payload={'step_run_id': 'step-abc', 'other': 1},
    )
    e_other_step = new_root_log_event(
        level=LogLevel.INFO,
        message='other-step',
        component='test',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='i',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='a',
        target_type=LogParticipantKind.SYSTEM,
        target_id='default',
        correlation_id='step-payload-test',
        payload={'step_run_id': 'step-xyz'},
    )
    e_no_step = new_root_log_event(
        level=LogLevel.INFO,
        message='no-step',
        component='test',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='i',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='a',
        target_type=LogParticipantKind.SYSTEM,
        target_id='default',
        correlation_id='step-payload-test',
        payload={},
    )
    async with session_factory() as session:
        for e in (e_match, e_other_step, e_no_step):
            await insert_buffered_log_event(session, e)
        await session.commit()

    r = await client.get(
        '/api/v0/log-buffer',
        params={'payload_step_run_id': 'step-abc'},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'step-tagged'
