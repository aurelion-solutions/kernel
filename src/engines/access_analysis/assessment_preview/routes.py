# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Detector REST endpoints — POST /access-analysis/detect-orphans, POST /access-analysis/detect-terminated."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from src.core.http.errors import translate_service_errors
from src.engines.access_analysis.assessment_preview.deps import (
    get_orphan_detector_service,
    get_terminated_detector_service,
    get_unused_detector_service,
)
from src.engines.access_analysis.assessment_preview.schemas import (
    DetectOrphansRequest,
    DetectTerminatedRequest,
    DetectUnusedRequest,
    OrphanFindingResponse,
    TerminatedFindingResponse,
    UnusedFindingResponse,
)
from src.engines.access_analysis.assessment_preview.service import (
    OrphanDetectorService,
    TerminatedDetectorService,
    UnusedDetectorService,
)

router = APIRouter(prefix='/access-analysis', tags=['access-analysis'])

DependsOrphanDetector = Depends(get_orphan_detector_service)
DependsTerminatedDetector = Depends(get_terminated_detector_service)
DependsUnusedDetector = Depends(get_unused_detector_service)


@router.post('/detect-orphans', response_model=list[OrphanFindingResponse])
async def detect_orphans(
    body: DetectOrphansRequest,
    service: OrphanDetectorService = DependsOrphanDetector,
) -> list[OrphanFindingResponse]:
    """Detect orphan accounts (subject_id IS NULL) and return finding drafts.

    Returns a sorted list of OrphanFindingResponse. Empty list when no orphans exist.
    Never persists — read-only detection. No events emitted.
    Optional ``application_id`` scopes the scan to a single application.
    ``limit`` caps the number of candidate accounts loaded (default 1000, max 5000).
    """
    with translate_service_errors({}):
        findings = await service.run(
            application_id=body.application_id,
            limit=body.limit,
        )

    return [OrphanFindingResponse.from_orphan_finding(f) for f in findings]


@router.post('/detect-terminated', response_model=list[TerminatedFindingResponse])
async def detect_terminated(
    body: DetectTerminatedRequest,
    service: TerminatedDetectorService = DependsTerminatedDetector,
) -> list[TerminatedFindingResponse]:
    """Detect accounts linked to subjects in a terminal status and return finding drafts.

    Returns a sorted list of TerminatedFindingResponse. Empty list when none found.
    Never persists — read-only detection. No events emitted.
    Optional ``application_id`` scopes the scan to a single application.
    ``limit`` caps the number of candidate accounts loaded (default 1000, max 5000).
    """
    with translate_service_errors({}):
        findings = await service.run(
            application_id=body.application_id,
            limit=body.limit,
        )

    return [TerminatedFindingResponse.from_terminated_finding(f) for f in findings]


@router.post('/detect-unused', response_model=list[UnusedFindingResponse])
async def detect_unused(
    body: DetectUnusedRequest,
    service: UnusedDetectorService = DependsUnusedDetector,
) -> list[UnusedFindingResponse]:
    """Detect active access facts with stale or absent usage telemetry and return finding drafts.

    Returns a sorted list of UnusedFindingResponse. Empty list when no unused access exists.
    Never persists — read-only detection. No events emitted.
    Optional ``application_id`` scopes the scan to a single application.
    ``threshold_days`` sets the minimum elapsed days to qualify as unused (default 90, range 1–3650).
    ``limit`` caps the number of candidate access facts loaded (default 1000, max 5000).
    """
    with translate_service_errors({}):
        findings = await service.run(
            application_id=body.application_id,
            threshold_days=body.threshold_days,
            limit=body.limit,
        )

    return [UnusedFindingResponse.from_unused_finding(f) for f in findings]
