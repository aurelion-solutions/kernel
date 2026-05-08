# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanRun CRUD/status routes.

Endpoints:
  POST   /scan-runs                        — create (inserts pending run)
  GET    /scan-runs                        — list (filters: status, triggered_by,
                                                   scope_subject_id, scope_application_id)
  GET    /scan-runs/{scan_run_id}          — get by id
  PATCH  /scan-runs/{scan_run_id}/status   — transition status

Execution (POST /scan-runs/{id}/run) lives in engines/access_analysis/scan_routes.py.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.inventory.assessment.scan_runs.deps import get_scan_run_service
from src.inventory.assessment.scan_runs.exceptions import (
    ScanRunApplicationNotFoundError,
    ScanRunMissingErrorMessageError,
    ScanRunNotFoundError,
    ScanRunStatusTransitionError,
    ScanRunSubjectNotFoundError,
)
from src.inventory.assessment.scan_runs.models import ScanRunStatus, ScanRunTrigger
from src.inventory.assessment.scan_runs.schemas import (
    ScanRunCreate,
    ScanRunRead,
    ScanRunStatusPatch,
)
from src.inventory.assessment.scan_runs.service import ScanRunService

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
