# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependency for resolving the in-memory event buffer from app state."""

from fastapi import Request
from src.platform.events.buffer import InMemoryEventBuffer


def get_event_buffer(request: Request) -> InMemoryEventBuffer:
    """Resolve the ``InMemoryEventBuffer`` from ``app.state``.

    In production the buffer is created in the lifespan and stored on
    ``app.state.event_buffer``.  In tests that do not run a lifespan, a
    fresh empty buffer is created on demand so that endpoint handlers always
    have a deterministic, non-failing dependency.
    """
    if not hasattr(request.app.state, 'event_buffer'):
        request.app.state.event_buffer = InMemoryEventBuffer()
    return request.app.state.event_buffer  # type: ignore[no-any-return]
