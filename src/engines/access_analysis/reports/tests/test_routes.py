# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Route tests for the reports endpoint.

Strategy: mock ReportService via dependency override -- no live DB needed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.access_analysis.analytics.schemas import FindingsSummary
from src.engines.access_analysis.reports.deps import get_report_service
from src.engines.access_analysis.reports.routes import router as reports_router
from src.engines.access_analysis.reports.schemas import (
    DeterministicReport,
    EvidenceSnippet,
    ExecutiveSummaryBlock,
    Recommendation,
    TopFinding,
)
from src.engines.access_analysis.reports.service import ReportService

_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)


def _make_app(service_override: ReportService | None = None) -> FastAPI:
    noop_session = MagicMock(spec=AsyncSession)

    async def override_session() -> AsyncGenerator[AsyncSession]:
        yield noop_session

    app = FastAPI()
    app.include_router(reports_router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_session

    if service_override is not None:
        app.dependency_overrides[get_report_service] = lambda: service_override

    return app


def _make_findings_summary() -> FindingsSummary:
    return FindingsSummary(
        total_findings=1,
        findings_by_severity={'critical': 1, 'high': 0, 'medium': 0, 'low': 0, 'informational': 0},
        findings_by_kind={
            'sod': 0,
            'orphan_access': 1,
            'terminated_access': 0,
            'unused_access': 0,
            'privileged_access': 0,
        },
        critical_findings=1,
        high_findings=0,
        top_applications=[],
        top_subjects=[],
        quick_wins=[],
        generated_at=_NOW,
    )


def _make_deterministic_report() -> DeterministicReport:
    summary = _make_findings_summary()
    evidence = EvidenceSnippet(
        subject_external_id=None,
        account_username='alice@ad',
        application_id=uuid.uuid4(),
        application_code='AD',
    )
    top_finding = TopFinding(
        finding_id=1,
        kind='orphan_access',
        severity='critical',
        subject_id=None,
        account_id=uuid.uuid4(),
        detected_at=_NOW,
        evidence=evidence,
    )
    exec_blocks = [
        ExecutiveSummaryBlock(block_id='posture_overview', title='Posture Overview', body='Total: 1.', metric=1),
        ExecutiveSummaryBlock(block_id='top_risks', title='Top Risks', body='Count: 1.', metric=1),
        ExecutiveSummaryBlock(block_id='quick_wins_overview', title='Quick Wins', body='Count: 0.', metric=0),
        ExecutiveSummaryBlock(block_id='application_hotspots', title='App Hotspots', body='None.', metric=None),
        ExecutiveSummaryBlock(block_id='subject_hotspots', title='Subject Hotspots', body='None.', metric=None),
    ]
    return DeterministicReport(
        summary=summary,
        top_findings=[top_finding],
        recommendations=[
            Recommendation(
                kind='revoke_orphan_access',
                finding_kind='orphan_access',
                severity_floor='high',
                affected_finding_count=1,
                text='Revoke 1 orphan-access grant(s). Finding kind: orphan_access.',
            )
        ],
        executive_summary=exec_blocks,
        generated_at=_NOW,
    )


@pytest.mark.asyncio
async def test_get_deterministic_report_happy_path() -> None:
    """GET /api/v0/reports/deterministic returns 200 with expected envelope shape."""
    mock_svc = MagicMock(spec=ReportService)
    mock_svc.get_deterministic_report = AsyncMock(return_value=_make_deterministic_report())
    app = _make_app(service_override=mock_svc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        response = await client.get('/api/v0/reports/deterministic')

    assert response.status_code == 200
    data = response.json()
    assert 'summary' in data
    assert 'top_findings' in data
    assert 'recommendations' in data
    assert 'executive_summary' in data
    assert 'generated_at' in data
    assert len(data['top_findings']) >= 1
    assert len(data['executive_summary']) == 5
    assert data['summary']['total_findings'] >= 1


@pytest.mark.asyncio
async def test_get_deterministic_report_query_param_bounds() -> None:
    """top_findings_limit=0 and top_findings_limit=101 both return 422."""
    app = _make_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        r_low = await client.get('/api/v0/reports/deterministic', params={'top_findings_limit': 0})
        r_high = await client.get('/api/v0/reports/deterministic', params={'top_findings_limit': 101})

    assert r_low.status_code == 422
    assert r_high.status_code == 422
