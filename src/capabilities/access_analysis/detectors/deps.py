# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for detector services."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.detectors.service import (
    OrphanDetectorService,
    TerminatedDetectorService,
    UnusedDetectorService,
)
from src.core.db.deps import get_db
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


async def get_orphan_detector_service(
    session: AsyncSession = _DependsDB,
) -> OrphanDetectorService:
    """Return an OrphanDetectorService bound to the request session."""
    return OrphanDetectorService(session)


async def get_terminated_detector_service(
    session: AsyncSession = _DependsDB,
) -> TerminatedDetectorService:
    """Return a TerminatedDetectorService bound to the request session."""
    return TerminatedDetectorService(session)


async def get_lake_settings(lake_session: LakeSession = Depends(get_lake_session)) -> LakeSettings:  # noqa: B008
    """Return LakeSettings from environment (env-aware, no app state required)."""
    return LakeSettings()


async def get_unused_detector_service(
    session: AsyncSession = _DependsDB,
    lake_session: LakeSession = Depends(get_lake_session),  # noqa: B008
    log_service: LogService = _DependsLogs,
    settings: LakeSettings = Depends(get_lake_settings),  # noqa: B008
) -> UnusedDetectorService:
    """Return an UnusedDetectorService backed by DuckDB iceberg_scan."""
    return UnusedDetectorService(
        session=session,
        lake_session=lake_session,
        log_service=log_service,
        pg_any_array_max_size=settings.pg_any_array_max_size,
    )
