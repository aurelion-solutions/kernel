# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the access_analysis.reports slice.

Actions registered here wrap ReportService without touching service.py,
routes.py, or any other existing file. Registration happens at import time
via @register_action decorator side effects.

Note: get_report_service (deps.py) requires request.app.state which is not
available in ActionContext. ReportService is constructed inline here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from src.engines.access_analysis.analytics.service import AnalyticsService
from src.engines.access_analysis.reports.schemas import DeterministicReport
from src.engines.access_analysis.reports.service import ReportService
from src.platform.orchestrator.registry import ActionContext, register_action


class DeterministicReportArgs(BaseModel):
    """Args for access_analysis.reports.deterministic action."""

    model_config = ConfigDict(extra='forbid')

    top_findings_limit: int = Field(default=20, ge=1, le=100)
    summary_top_n: int = Field(default=10, ge=1, le=100)
    summary_quick_wins_limit: int = Field(default=50, ge=1, le=500)


@register_action(  # type: ignore[arg-type]
    engine='access_analysis.reports',
    action='deterministic',
    args_schema=DeterministicReportArgs,
    result_schema=DeterministicReport,
    idempotent=True,
)
async def reports_deterministic(
    args: DeterministicReportArgs,
    ctx: ActionContext,
) -> DeterministicReport:
    """Assemble and return a product-neutral deterministic report payload."""
    analytics_service = AnalyticsService(log_service=ctx.log_service)
    service = ReportService(analytics_service=analytics_service, log_service=ctx.log_service)
    return await service.get_deterministic_report(
        ctx.session,
        top_findings_limit=args.top_findings_limit,
        summary_top_n=args.summary_top_n,
        summary_quick_wins_limit=args.summary_quick_wins_limit,
    )
