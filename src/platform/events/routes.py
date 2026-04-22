# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Read endpoint for the in-memory domain event ring buffer."""

from fastapi import APIRouter, Depends, Query
from src.platform.events.buffer import InMemoryEventBuffer
from src.platform.events.deps import get_event_buffer
from src.platform.events.read_schemas import EventBufferEntryRead

router = APIRouter(prefix='/platform/events', tags=['platform-events'])

_q_limit = Query(50, ge=1, le=500, description='Max rows (1..500).')
_DependsBuffer = Depends(get_event_buffer)


@router.get('', response_model=list[EventBufferEntryRead])
async def list_recent_events(
    limit: int = _q_limit,
    buffer: InMemoryEventBuffer = _DependsBuffer,
) -> list[EventBufferEntryRead]:
    """Return the most recent domain events from the in-memory ring buffer, newest first."""
    envs = buffer.snapshot(limit=limit)
    return [EventBufferEntryRead.from_envelope(e) for e in envs]
