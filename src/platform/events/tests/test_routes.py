# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for GET /api/v0/platform/events."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from src.platform.events.buffer import InMemoryEventBuffer
from src.platform.events.schemas import EventEnvelope


def _envelope(occurred_at: datetime | None = None) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type='test.entity.created',
        occurred_at=occurred_at or datetime.now(UTC),
        correlation_id=str(uuid4()),
    )


@pytest.mark.asyncio
async def test_list_events_empty_returns_200_and_empty_list(client) -> None:
    r = await client.get('/api/v0/platform/events')
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_list_events_returns_newest_first_and_respects_limit(app, client) -> None:
    buf: InMemoryEventBuffer = app.state.event_buffer
    e1 = _envelope()
    e2 = _envelope()
    e3 = _envelope()
    buf.append(e1)
    buf.append(e2)
    buf.append(e3)

    r = await client.get('/api/v0/platform/events', params={'limit': 2})
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    # Newest first: e3, then e2
    assert data[0]['event_id'] == str(e3.event_id)
    assert data[1]['event_id'] == str(e2.event_id)


@pytest.mark.asyncio
async def test_list_events_limit_validation_422(client) -> None:
    r0 = await client.get('/api/v0/platform/events', params={'limit': 0})
    assert r0.status_code == 422

    r501 = await client.get('/api/v0/platform/events', params={'limit': 501})
    assert r501.status_code == 422
