# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for GET /api/v0/platform/logs."""

import pytest
from src.platform.logs.buffer_repository import insert_buffered_log_event
from src.platform.logs.schemas import LogLevel, LogParticipantKind, new_root_log_event


def _root(
    *,
    correlation_id: str = 'test-trace',
    message: str = 'msg',
    level: LogLevel = LogLevel.INFO,
) -> object:
    return new_root_log_event(
        level=level,
        message=message,
        component='test',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='system',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='system',
        target_type=LogParticipantKind.SYSTEM,
        target_id='target',
        correlation_id=correlation_id,
    )


@pytest.mark.asyncio
async def test_list_recent_logs_empty_returns_empty_list(client) -> None:
    r = await client.get('/api/v0/platform/logs')
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_recent_logs_returns_newest_first_and_respects_limit(client, session_factory) -> None:
    async with session_factory() as session:
        for i in range(5):
            await insert_buffered_log_event(session, _root(message=f'msg-{i}'))
        await session.commit()

    r = await client.get('/api/v0/platform/logs', params={'limit': 3})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3


@pytest.mark.asyncio
async def test_list_recent_logs_filters_by_level(client, session_factory) -> None:
    async with session_factory() as session:
        await insert_buffered_log_event(session, _root(message='info-1', level=LogLevel.INFO))
        await insert_buffered_log_event(session, _root(message='info-2', level=LogLevel.INFO))
        await insert_buffered_log_event(session, _root(message='error-1', level=LogLevel.ERROR))
        await session.commit()

    r = await client.get('/api/v0/platform/logs', params={'level': 'error'})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]['message'] == 'error-1'
