# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Scan execution route — POST /scan-runs/{scan_run_id}/run.

Owned by access_analysis because execution depends on ScanOrchestrationService.
CRUD/status routes remain in inventory/assessment/scan_runs/routes.py.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.engines.access_analysis.service import ScanOrchestrationService
from src.inventory.assessment.scan_runs.exceptions import (
    ScanRunNotFoundError,
    ScanRunStatusTransitionError,
)
from src.inventory.assessment.scan_runs.schemas import ScanRunRead
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

router = APIRouter(prefix='/scan-runs', tags=['scan-runs'])


@router.post('/{scan_run_id}/run', response_model=ScanRunRead)
async def run_scan_run(
    scan_run_id: int,
    session: AsyncSession = Depends(get_db),  # noqa: B008
    lake_session: LakeSession = Depends(get_lake_session),  # noqa: B008
    log_service: LogService = Depends(get_log_service),  # noqa: B008
) -> ScanRunRead:
    """Execute a pending ScanRun synchronously.

    Returns 404 if the ScanRun does not exist.
    Returns 409 if the ScanRun is not in pending status.
    """
    settings = LakeSettings()
    orch = ScanOrchestrationService(
        session=session,
        lake_session=lake_session,
        log_service=log_service,
        pg_any_array_max_size=settings.pg_any_array_max_size,
    )
    with translate_service_errors(
        {
            ScanRunNotFoundError: (404, 'ScanRun not found'),
            ScanRunStatusTransitionError: (409, lambda e: str(e)),
        }
    ):
        scan_run = await orch.run_scan(scan_run_id)
    await session.commit()
    return ScanRunRead.model_validate(scan_run)
