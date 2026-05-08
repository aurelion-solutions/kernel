# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route tests for analytics endpoints.

Strategy: mock AnalyticsService via dependency override — no live DB needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.access_analysis.analytics.deps import get_analytics_service
from src.engines.access_analysis.analytics.routes import router as analytics_router
from src.engines.access_analysis.analytics.schemas import (
    FindingsSummary,
    RiskByApplicationItem,
    RiskByApplicationResponse,
    TopApplicationFindingCount,
    TopRiskItem,
    TopRisksResponse,
)
from src.engines.access_analysis.analytics.service import AnalyticsService
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession

_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)


def _make_app(service_override: AnalyticsService | None = None) -> FastAPI:
    noop_lake = MagicMock(spec=LakeSession)
    noop_session = MagicMock(spec=AsyncSession)

    async def override_lake():
        yield noop_lake

    async def override_session():
        yield noop_session

    app = FastAPI()
    app.include_router(analytics_router, prefix='/api/v0')
    app.dependency_overrides[get_lake_session] = override_lake
    app.dependency_overrides[get_db] = override_session

    if service_override is not None:
        app.dependency_overrides[get_analytics_service] = lambda: service_override

    return app


def _make_top_risks_response(n: int = 2) -> TopRisksResponse:
    return TopRisksResponse(
        generated_at=_NOW,
        items=[
            TopRiskItem(
                subject_id=uuid.uuid4(),
                application_id=uuid.uuid4(),
                risk_score=100 - i * 10,
                open_findings_count=5 - i,
                severity_breakdown={'critical': 1, 'high': 0, 'medium': 0, 'low': 0, 'informational': 0},
            )
            for i in range(n)
        ],
    )


def _make_findings_summary_response() -> FindingsSummary:
    return FindingsSummary(
        total_findings=3,
        findings_by_severity={'critical': 1, 'high': 2, 'medium': 0, 'low': 0, 'informational': 0},
        findings_by_kind={
            'sod': 0,
            'orphan_access': 1,
            'terminated_access': 1,
            'unused_access': 1,
            'privileged_access': 0,
        },
        critical_findings=1,
        high_findings=2,
        top_applications=[TopApplicationFindingCount(application_id=uuid.uuid4(), finding_count=2)],
        top_subjects=[],
        quick_wins=[],
        generated_at=_NOW,
    )


def _make_rba_response(n: int = 2) -> RiskByApplicationResponse:
    return RiskByApplicationResponse(
        generated_at=_NOW,
        items=[
            RiskByApplicationItem(
                application_id=uuid.uuid4(),
                risk_score=200 - i * 50,
                open_findings_count=4 - i,
                severity_breakdown={'critical': 0, 'high': 2, 'medium': 0, 'low': 0, 'informational': 0},
            )
            for i in range(n)
        ],
    )


# ---------------------------------------------------------------------------
# top-risks tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_top_risks_default_limit_returns_200() -> None:
    """GET /analytics/top-risks with default limit returns 200 and items."""
    mock_svc = MagicMock(spec=AnalyticsService)
    mock_svc.get_top_risks = AsyncMock(return_value=_make_top_risks_response(2))
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/analytics/top-risks')

    assert response.status_code == 200
    data = response.json()
    assert 'items' in data
    assert 'generated_at' in data
    assert len(data['items']) == 2


@pytest.mark.asyncio
async def test_get_top_risks_limit_zero_returns_422() -> None:
    """GET /analytics/top-risks?limit=0 → 422 (ge=1 constraint)."""
    app = _make_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/analytics/top-risks', params={'limit': 0})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_top_risks_limit_over_100_returns_422() -> None:
    """GET /analytics/top-risks?limit=101 → 422 (le=100 constraint)."""
    app = _make_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/analytics/top-risks', params={'limit': 101})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_get_top_risks_custom_limit_passed_to_service() -> None:
    """GET /analytics/top-risks?limit=5 calls service with limit=5."""
    mock_svc = MagicMock(spec=AnalyticsService)
    mock_svc.get_top_risks = AsyncMock(return_value=_make_top_risks_response(0))
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        await client.get('/api/v0/analytics/top-risks', params={'limit': 5})

    mock_svc.get_top_risks.assert_called_once()
    _, kwargs = mock_svc.get_top_risks.call_args
    assert kwargs.get('limit') == 5


# ---------------------------------------------------------------------------
# risk-by-application tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_risk_by_application_returns_200_with_items_and_generated_at() -> None:
    """GET /analytics/risk-by-application returns 200 with items and generated_at."""
    mock_svc = MagicMock(spec=AnalyticsService)
    mock_svc.get_risk_by_application = AsyncMock(return_value=_make_rba_response(2))
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/analytics/risk-by-application')

    assert response.status_code == 200
    data = response.json()
    assert 'items' in data
    assert 'generated_at' in data
    assert len(data['items']) == 2
    item = data['items'][0]
    assert 'application_id' in item
    assert 'risk_score' in item
    assert 'severity_breakdown' in item


# ---------------------------------------------------------------------------
# findings-summary tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_findings_summary_returns_200_with_envelope_shape() -> None:
    """GET /analytics/findings-summary returns 200 with expected envelope fields."""
    mock_svc = MagicMock(spec=AnalyticsService)
    mock_svc.get_findings_summary = AsyncMock(return_value=_make_findings_summary_response())
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/analytics/findings-summary')

    assert response.status_code == 200
    data = response.json()
    assert 'total_findings' in data
    assert 'findings_by_severity' in data
    assert 'findings_by_kind' in data
    assert 'critical_findings' in data
    assert 'high_findings' in data
    assert 'top_applications' in data
    assert 'top_subjects' in data
    assert 'quick_wins' in data
    assert 'generated_at' in data
    assert data['total_findings'] == 3
    assert data['critical_findings'] == 1
    assert data['high_findings'] == 2
    assert len(data['top_applications']) == 1
    mock_svc.get_findings_summary.assert_called_once()
    _, kwargs = mock_svc.get_findings_summary.call_args
    assert kwargs.get('top_n') == 10
    assert kwargs.get('quick_wins_limit') == 50
