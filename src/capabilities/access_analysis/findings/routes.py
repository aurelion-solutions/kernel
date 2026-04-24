# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Finding API routes.

Endpoints:
  GET    /findings                        — list (filters: scan_run_id, rule_id, severity, status, kind, subject_id)
  GET    /findings/{finding_id}           — get by id
  PATCH  /findings/{finding_id}/status    — transition status
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from src.capabilities.access_analysis.findings.deps import get_finding_service
from src.capabilities.access_analysis.findings.exceptions import (
    FindingMissingReasonError,
    FindingMitigationLinkageMissingError,
    FindingMitigationNotApplicableError,
    FindingNotFoundError,
    FindingStatusTransitionError,
)
from src.capabilities.access_analysis.findings.models import FindingKind, FindingStatus
from src.capabilities.access_analysis.findings.schemas import FindingRead, FindingStatusPatch
from src.capabilities.access_analysis.findings.service import FindingService
from src.capabilities.access_analysis.sod_rules.models import SodSeverity
from src.core.http.errors import translate_service_errors

router = APIRouter(prefix='/findings', tags=['findings'])
DependsService = Depends(get_finding_service)


@router.get('', response_model=list[FindingRead])
async def list_findings(
    scan_run_id: int | None = None,
    rule_id: int | None = None,
    severity: SodSeverity | None = None,
    status: FindingStatus | None = None,
    kind: FindingKind | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
    service: FindingService = DependsService,
) -> list[FindingRead]:
    """List Findings, optionally filtered. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(
        scan_run_id=scan_run_id,
        rule_id=rule_id,
        severity=severity,
        status=status,
        kind=kind,
        subject_id=subject_id,
        limit=effective_limit,
        offset=offset,
    )


@router.get('/{finding_id}', response_model=FindingRead)
async def get_finding(
    finding_id: int,
    service: FindingService = DependsService,
) -> FindingRead:
    """Get a Finding by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            FindingNotFoundError: (404, 'Finding not found'),
        }
    ):
        result = await service.get(finding_id)
    return result


@router.patch('/{finding_id}/status', response_model=FindingRead)
async def patch_finding_status(
    finding_id: int,
    body: FindingStatusPatch,
    service: FindingService = DependsService,
) -> FindingRead:
    """Transition the status of a Finding. Returns 422 on illegal transition."""
    with translate_service_errors(
        {
            FindingNotFoundError: (404, 'Finding not found'),
            FindingStatusTransitionError: (422, lambda e: str(e)),
            FindingMissingReasonError: (422, lambda e: str(e)),
            FindingMitigationLinkageMissingError: (422, lambda e: str(e)),
            FindingMitigationNotApplicableError: (422, lambda e: str(e)),
        }
    ):
        result = await service.patch_status(finding_id, body)
    return result
