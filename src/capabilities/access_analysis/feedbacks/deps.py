# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FeedbackService FastAPI dependency.

EventService is built per-request using the event_sink_factory pattern from
src.capabilities.reconciliation.deps — there is no global get_event_service
function in platform/events/deps.py.
"""

from __future__ import annotations

import os

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.feedbacks.service import FeedbackService
from src.core.db.deps import get_db
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService

_DependsDB = Depends(get_db)


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


async def get_feedback_service(
    session: AsyncSession = _DependsDB,
) -> FeedbackService:
    """Return a FeedbackService bound to the request session and event service."""
    event_sink = event_sink_factory.get(_get_events_provider())
    event_service = EventService(sink=event_sink)
    return FeedbackService(session, event_service)
