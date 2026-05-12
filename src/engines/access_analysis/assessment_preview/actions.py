# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the assessment_preview slice.

Actions registered here wrap OrphanDetectorService and TerminatedDetectorService
without touching service.py, routes.py, or any other existing file.
Registration happens at import time via @register_action decorator side effects.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from src.engines.access_analysis.assessment_preview.schemas import (
    DetectOrphansRequest,
    DetectTerminatedRequest,
    OrphanFindingResponse,
    TerminatedFindingResponse,
)
from src.engines.access_analysis.assessment_preview.service import (
    OrphanDetectorService,
    TerminatedDetectorService,
)
from src.platform.orchestrator.registry import ActionContext, register_action


class DetectOrphansResult(BaseModel):
    """Result wrapper for assessment_preview.detect_orphans action."""

    model_config = ConfigDict(frozen=True)

    findings: list[OrphanFindingResponse]


class DetectTerminatedResult(BaseModel):
    """Result wrapper for assessment_preview.detect_terminated action."""

    model_config = ConfigDict(frozen=True)

    findings: list[TerminatedFindingResponse]


@register_action(  # type: ignore[arg-type]
    engine='access_analysis.assessment_preview',
    action='detect_orphans',
    args_schema=DetectOrphansRequest,
    result_schema=DetectOrphansResult,
    idempotent=True,
)
async def assessment_preview_detect_orphans(
    args: DetectOrphansRequest,
    ctx: ActionContext,
) -> DetectOrphansResult:
    """Detect orphan accounts (subject_id IS NULL) within optional application scope."""
    service = OrphanDetectorService(ctx.session)
    findings = await service.run(application_id=args.application_id, limit=args.limit)
    return DetectOrphansResult(findings=[OrphanFindingResponse.from_orphan_finding(f) for f in findings])


@register_action(  # type: ignore[arg-type]
    engine='access_analysis.assessment_preview',
    action='detect_terminated',
    args_schema=DetectTerminatedRequest,
    result_schema=DetectTerminatedResult,
    idempotent=True,
)
async def assessment_preview_detect_terminated(
    args: DetectTerminatedRequest,
    ctx: ActionContext,
) -> DetectTerminatedResult:
    """Detect accounts whose linked Subject is in a terminal status for its kind."""
    service = TerminatedDetectorService(ctx.session)
    findings = await service.run(application_id=args.application_id, limit=args.limit)
    return DetectTerminatedResult(findings=[TerminatedFindingResponse.from_terminated_finding(f) for f in findings])
