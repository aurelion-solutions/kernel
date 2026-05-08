# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the reports slice."""

from __future__ import annotations

from fastapi import Request
from src.engines.access_analysis.analytics.service import AnalyticsService
from src.engines.access_analysis.reports.service import ReportService


def get_report_service(request: Request) -> ReportService:
    """Return ReportService with injected LogService from app state."""
    log_service = getattr(request.app.state, 'log_service', None)
    analytics_service = AnalyticsService(log_service=log_service)
    return ReportService(analytics_service=analytics_service, log_service=log_service)
