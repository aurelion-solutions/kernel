# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun route dependencies."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.assessment.scan_runs.service import ScanRunService
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


async def get_scan_run_service(
    session: AsyncSession = _DependsDB,
    log_service: LogService = _DependsLogs,
) -> ScanRunService:
    """Return a ScanRunService bound to the request session and log service."""
    return ScanRunService(session, log_service)
