# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reports API routes.

Endpoints:
  GET /reports/deterministic  — product-neutral deterministic report payload
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.access_analysis.reports.deps import get_report_service
from src.engines.access_analysis.reports.schemas import DeterministicReport
from src.engines.access_analysis.reports.service import ReportService

router = APIRouter(prefix='/reports', tags=['reports'])
DependsSession = Depends(get_db)
DependsService = Depends(get_report_service)


@router.get('/deterministic', response_model=DeterministicReport)
async def get_deterministic_report(
    top_findings_limit: int = Query(default=20, ge=1, le=100),
    summary_top_n: int = Query(default=10, ge=1, le=100),
    summary_quick_wins_limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = DependsSession,
    service: ReportService = DependsService,
) -> DeterministicReport:
    """Return a product-neutral deterministic report payload.

    Combines FindingsSummary (Step 20) with top high/critical findings and their
    evidence, rule-based recommendations, and five fixed executive summary blocks.
    No LLM, no PDF -- JSON only.
    """
    return await service.get_deterministic_report(
        session,
        top_findings_limit=top_findings_limit,
        summary_top_n=summary_top_n,
        summary_quick_wins_limit=summary_quick_wins_limit,
    )
