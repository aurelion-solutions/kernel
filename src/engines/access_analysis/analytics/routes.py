# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Analytics API routes.

Endpoints:
  GET /analytics/top-risks?limit=N              — top-N (subject, app) by risk score
  GET /analytics/risk-by-application            — risk aggregated per application
  GET /analytics/findings-summary?top_n&quick_wins_limit — PG-only findings digest
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.access_analysis.analytics.deps import get_analytics_service
from src.engines.access_analysis.analytics.schemas import (
    FindingsSummary,
    RiskByApplicationResponse,
    TopRisksResponse,
)
from src.engines.access_analysis.analytics.service import AnalyticsService
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession

router = APIRouter(prefix='/analytics', tags=['analytics'])
DependsService = Depends(get_analytics_service)
DependsLakeSession = Depends(get_lake_session)
DependsSession = Depends(get_db)


@router.get('/top-risks', response_model=TopRisksResponse)
async def get_top_risks(
    limit: int = Query(default=10, ge=1, le=100),
    lake_session: LakeSession = DependsLakeSession,
    service: AnalyticsService = DependsService,
) -> TopRisksResponse:
    """Return top-N (subject, application) pairs by risk score.

    Risk score = Σ(severity_weight × open_findings_count_per_severity).
    Weights: critical=100, high=50, medium=20, low=5.
    Sorted: risk_score DESC, then (subject_id, application_id) ASC.
    """
    return await service.get_top_risks(lake_session, limit=limit)


@router.get('/risk-by-application', response_model=RiskByApplicationResponse)
async def get_risk_by_application(
    lake_session: LakeSession = DependsLakeSession,
    service: AnalyticsService = DependsService,
) -> RiskByApplicationResponse:
    """Return risk aggregated per application.

    Sorted: risk_score DESC, then application_id ASC.
    """
    return await service.get_risk_by_application(lake_session)


@router.get('/findings-summary', response_model=FindingsSummary)
async def get_findings_summary(
    top_n: int = Query(default=10, ge=1, le=100),
    quick_wins_limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = DependsSession,
    service: AnalyticsService = DependsService,
) -> FindingsSummary:
    """Return a PG-only digest of open findings.

    Counts open findings, breaks them down by severity and kind, and lists
    top applications, top subjects, and quick-win candidates.
    Findings with NULL account_id are excluded from top_applications
    (subject-only findings cannot be attributed to an application).
    """
    return await service.get_findings_summary(session, top_n=top_n, quick_wins_limit=quick_wins_limit)
