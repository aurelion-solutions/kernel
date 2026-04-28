# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun API routes.

Endpoints:
  POST   /scan-runs                        — create (inserts pending run)
  GET    /scan-runs                        — list (filters: status, triggered_by,
                                                   scope_subject_id, scope_application_id)
  GET    /scan-runs/{scan_run_id}          — get by id
  PATCH  /scan-runs/{scan_run_id}/status   — transition status
  POST   /scan-runs/{scan_run_id}/run      — execute a pending run (synchronous)
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.scan_runs.deps import get_scan_run_service
from src.capabilities.access_analysis.scan_runs.exceptions import (
    ScanRunApplicationNotFoundError,
    ScanRunMissingErrorMessageError,
    ScanRunNotFoundError,
    ScanRunStatusTransitionError,
    ScanRunSubjectNotFoundError,
)
from src.capabilities.access_analysis.scan_runs.models import ScanRunStatus, ScanRunTrigger
from src.capabilities.access_analysis.scan_runs.schemas import (
    ScanRunCreate,
    ScanRunRead,
    ScanRunStatusPatch,
)
from src.capabilities.access_analysis.scan_runs.service import ScanRunService
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

router = APIRouter(prefix='/scan-runs', tags=['scan-runs'])
DependsService = Depends(get_scan_run_service)


@router.post('', response_model=ScanRunRead, status_code=201)
async def create_scan_run(
    body: ScanRunCreate,
    session: AsyncSession = Depends(get_db),  # noqa: B008
    service: ScanRunService = DependsService,
) -> ScanRunRead:
    """Create a new pending ScanRun."""
    with translate_service_errors(
        {
            ScanRunSubjectNotFoundError: (422, lambda e: f'Subject {e.subject_id} not found'),
            ScanRunApplicationNotFoundError: (422, lambda e: f'Application {e.application_id} not found'),
        }
    ):
        result = await service.create(body)
    await session.commit()
    return result


@router.get('', response_model=list[ScanRunRead])
async def list_scan_runs(
    status: ScanRunStatus | None = None,
    triggered_by: ScanRunTrigger | None = None,
    scope_subject_id: uuid.UUID | None = None,
    scope_application_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
    service: ScanRunService = DependsService,
) -> list[ScanRunRead]:
    """List ScanRuns, optionally filtered. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(
        status=status,
        triggered_by=triggered_by,
        scope_subject_id=scope_subject_id,
        scope_application_id=scope_application_id,
        limit=effective_limit,
        offset=offset,
    )


@router.get('/{scan_run_id}', response_model=ScanRunRead)
async def get_scan_run(
    scan_run_id: int,
    service: ScanRunService = DependsService,
) -> ScanRunRead:
    """Get a ScanRun by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            ScanRunNotFoundError: (404, 'ScanRun not found'),
        }
    ):
        result = await service.get(scan_run_id)
    return result


@router.patch('/{scan_run_id}/status', response_model=ScanRunRead)
async def patch_scan_run_status(
    scan_run_id: int,
    body: ScanRunStatusPatch,
    session: AsyncSession = Depends(get_db),  # noqa: B008
    service: ScanRunService = DependsService,
) -> ScanRunRead:
    """Transition the status of a ScanRun. Returns 422 on illegal transition."""
    with translate_service_errors(
        {
            ScanRunNotFoundError: (404, 'ScanRun not found'),
            ScanRunStatusTransitionError: (422, lambda e: str(e)),
            ScanRunMissingErrorMessageError: (422, lambda e: str(e)),
        }
    ):
        result = await service.patch_status(scan_run_id, body)
    await session.commit()
    return result


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
    from src.capabilities.access_analysis.service import ScanOrchestrationService

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
