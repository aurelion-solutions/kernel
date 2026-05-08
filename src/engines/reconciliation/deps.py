# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ReconciliationService FastAPI dependency."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.reconciliation.service import ReconciliationService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.lake.deps import get_lake_catalog, get_lake_session
from src.platform.logs.deps import get_log_service

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from src.platform.lake.duckdb_session import LakeSession
    from src.platform.logs.service import LogService, NoOpLogService


def _build_event_service() -> EventService:
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'noop')
    sink = event_sink_factory.get(provider)
    return EventService(sink=sink)


_DependsDB = Depends(get_db)
_DependsLakeSession = Depends(get_lake_session)
_DependsLakeCatalog = Depends(get_lake_catalog)


async def get_reconciliation_service(
    request: Request,
    session: AsyncSession = _DependsDB,
    lake_session: LakeSession = _DependsLakeSession,
    catalog: Catalog = _DependsLakeCatalog,
) -> ReconciliationService:
    """Build ReconciliationService with all five dependencies wired via FastAPI DI."""
    log_service: LogService | NoOpLogService = get_log_service(request)
    event_service = _build_event_service()
    return ReconciliationService(
        session=session,
        lake_session=lake_session,
        catalog=catalog,
        events=event_service,
        logs=log_service,
    )
