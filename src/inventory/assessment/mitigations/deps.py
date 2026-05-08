# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationService FastAPI dependency.

EventService is built per-request using platform event_sink_factory.
"""

from __future__ import annotations

import os

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.assessment.mitigations.service import MitigationService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


async def get_mitigation_service(
    session: AsyncSession = _DependsDB,
    log_service: LogService = _DependsLogs,
) -> MitigationService:
    """Return a MitigationService bound to the request session, log service, and event service."""
    event_sink = event_sink_factory.get(_get_events_provider())
    event_service = EventService(sink=event_sink)
    return MitigationService(session, log_service, event_service)
