# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the analytics slice."""

from __future__ import annotations

from fastapi import Request
from src.engines.access_analysis.analytics.service import AnalyticsService


def get_analytics_service(request: Request) -> AnalyticsService:
    """Return AnalyticsService with injected LogService from app state."""
    log_service = getattr(request.app.state, 'log_service', None)
    return AnalyticsService(log_service=log_service)
