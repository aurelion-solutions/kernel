# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ReportService — deterministic report payload assembled from PG findings.

Read-only. No events. No flush/commit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_analysis.analytics.schemas import FindingsSummary
from src.engines.access_analysis.analytics.service import AnalyticsService
from src.engines.access_analysis.reports.schemas import (
    DeterministicReport,
    EvidenceSnippet,
    ExecutiveSummaryBlock,
    Recommendation,
    TopFinding,
)
from src.inventory.accounts.models import Account
from src.inventory.assessment.findings.models import Finding, FindingStatus
from src.inventory.subjects.models import Subject
from src.platform.applications.models import Application
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_log_participant_fields

_COMPONENT = 'engines.access_analysis.reports'

# ---------------------------------------------------------------------------
# Severity rank — local string table, no SodSeverity import
# ---------------------------------------------------------------------------

_SEVERITY_RANK: dict[str, int] = {
    'critical': 0,
    'high': 1,
    'medium': 2,
    'low': 3,
}


def _severity_at_or_above(observed: str, floor: str) -> bool:
    """Return True if observed severity is >= floor (lower rank = higher severity)."""
    obs_rank = _SEVERITY_RANK.get(observed, 999)
    floor_rank = _SEVERITY_RANK.get(floor, 999)
    return obs_rank <= floor_rank


# ---------------------------------------------------------------------------
# Recommendation rule table
# ---------------------------------------------------------------------------


class _RecRule(NamedTuple):
    finding_kind: str
    recommendation_kind: str
    severity_floor: str


_RECOMMENDATION_RULES: tuple[_RecRule, ...] = (
    _RecRule('orphan_access', 'revoke_orphan_access', 'high'),
    _RecRule('terminated_access', 'revoke_terminated_access', 'high'),
    _RecRule('unused_access', 'review_unused_access', 'medium'),
    _RecRule('privileged_access', 'review_privileged_access', 'high'),
    _RecRule('sod', 'review_sod_violation', 'high'),
)

_RECOMMENDATION_TEMPLATES: dict[str, str] = {
    'revoke_orphan_access': 'Revoke {n} orphan-access grant(s). Finding kind: orphan_access.',
    'revoke_terminated_access': 'Revoke {n} terminated-subject access grant(s). Finding kind: terminated_access.',
    'review_unused_access': 'Review {n} unused-access grant(s) for potential revocation. Finding kind: unused_access.',
    'review_privileged_access': (
        'Review {n} privileged-access grant(s) for least-privilege compliance. Finding kind: privileged_access.'
    ),
    'review_sod_violation': 'Review {n} segregation-of-duties violation(s). Finding kind: sod.',
}

# ---------------------------------------------------------------------------
# Executive summary block templates
# ---------------------------------------------------------------------------

# block_id -> (title, body_template)
_EXECUTIVE_BLOCK_TEMPLATES: dict[str, tuple[str, str]] = {
    'posture_overview': (
        'Posture Overview',
        'Total open findings: {total}. Critical: {critical}. High: {high}.',
    ),
    'top_risks': (
        'Top Risks',
        'High-severity findings returned: {count}. Highest severity present: {highest}.',
    ),
    'quick_wins_overview': (
        'Quick Wins',
        'Quick-win candidates (high/critical orphan, terminated, unused access): {count}.',
    ),
    'application_hotspots': (
        'Application Hotspots',
        'Top application by finding count: {detail}.',
    ),
    'subject_hotspots': (
        'Subject Hotspots',
        'Top subject by finding count: {detail}.',
    ),
}


# ---------------------------------------------------------------------------
# Pure builder helpers
# ---------------------------------------------------------------------------


def _build_recommendations(
    summary: FindingsSummary,
    kind_severity_counts: dict[tuple[str, str], int],
) -> list[Recommendation]:
    """Build recommendations filtered by severity floor and sorted by priority."""
    result: list[Recommendation] = []
    for rule in _RECOMMENDATION_RULES:
        count = summary.findings_by_kind.get(rule.finding_kind, 0)
        if count == 0:
            continue
        # Check if at least one open finding meets the severity floor
        meets_floor = any(
            _severity_at_or_above(sev, rule.severity_floor)
            for (kind, sev), _ in kind_severity_counts.items()
            if kind == rule.finding_kind
        )
        if not meets_floor:
            continue
        text = _RECOMMENDATION_TEMPLATES[rule.recommendation_kind].format(n=count)
        result.append(
            Recommendation(
                kind=rule.recommendation_kind,
                finding_kind=rule.finding_kind,
                severity_floor=rule.severity_floor,
                affected_finding_count=count,
                text=text,
            )
        )
    # Sort: severity_floor priority ASC, then affected_finding_count DESC, then kind ASC
    result.sort(
        key=lambda r: (
            _SEVERITY_RANK.get(r.severity_floor, 999),
            -r.affected_finding_count,
            r.kind,
        )
    )
    return result


def _build_executive_summary(
    summary: FindingsSummary,
    top_findings: list[TopFinding],
) -> list[ExecutiveSummaryBlock]:
    """Build exactly five executive summary blocks in fixed order."""
    blocks: list[ExecutiveSummaryBlock] = []

    # 1. posture_overview
    title, body_tpl = _EXECUTIVE_BLOCK_TEMPLATES['posture_overview']
    body = body_tpl.format(
        total=summary.total_findings,
        critical=summary.critical_findings,
        high=summary.high_findings,
    )
    blocks.append(
        ExecutiveSummaryBlock(
            block_id='posture_overview',
            title=title,
            body=body,
            metric=summary.total_findings,
        )
    )

    # 2. top_risks
    title, body_tpl = _EXECUTIVE_BLOCK_TEMPLATES['top_risks']
    highest_sev = 'none'
    if top_findings:
        sev_list = [f.severity for f in top_findings]
        highest_sev = min(sev_list, key=lambda s: _SEVERITY_RANK.get(s, 999))
    body = body_tpl.format(count=len(top_findings), highest=highest_sev)
    blocks.append(
        ExecutiveSummaryBlock(
            block_id='top_risks',
            title=title,
            body=body,
            metric=len(top_findings),
        )
    )

    # 3. quick_wins_overview
    title, body_tpl = _EXECUTIVE_BLOCK_TEMPLATES['quick_wins_overview']
    body = body_tpl.format(count=len(summary.quick_wins))
    blocks.append(
        ExecutiveSummaryBlock(
            block_id='quick_wins_overview',
            title=title,
            body=body,
            metric=len(summary.quick_wins),
        )
    )

    # 4. application_hotspots
    title, body_tpl = _EXECUTIVE_BLOCK_TEMPLATES['application_hotspots']
    if summary.top_applications:
        top_app = summary.top_applications[0]
        detail = f'application {top_app.application_id} with {top_app.finding_count} finding(s)'
        metric: int | None = top_app.finding_count
    else:
        detail = 'no application hotspots identified'
        metric = None
    body = body_tpl.format(detail=detail)
    blocks.append(
        ExecutiveSummaryBlock(
            block_id='application_hotspots',
            title=title,
            body=body,
            metric=metric,
        )
    )

    # 5. subject_hotspots
    title, body_tpl = _EXECUTIVE_BLOCK_TEMPLATES['subject_hotspots']
    if summary.top_subjects:
        top_subj = summary.top_subjects[0]
        subj_detail = f'subject {top_subj.subject_id} with {top_subj.finding_count} finding(s)'
        subj_metric: int | None = top_subj.finding_count
    else:
        subj_detail = 'no subject hotspots identified'
        subj_metric = None
    body = body_tpl.format(detail=subj_detail)
    blocks.append(
        ExecutiveSummaryBlock(
            block_id='subject_hotspots',
            title=title,
            body=body,
            metric=subj_metric,
        )
    )

    return blocks


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReportService:
    """Assembles the deterministic report payload. Read-only."""

    def __init__(
        self,
        analytics_service: AnalyticsService,
        log_service: LogService | None = None,
    ) -> None:
        self._analytics = analytics_service
        self._log: LogService | NoOpLogService = log_service if log_service is not None else NoOpLogService()

    async def get_deterministic_report(
        self,
        session: AsyncSession,
        *,
        top_findings_limit: int = 20,
        summary_top_n: int = 10,
        summary_quick_wins_limit: int = 50,
    ) -> DeterministicReport:
        """Assemble and return a DeterministicReport envelope."""
        summary = await self._analytics.get_findings_summary(
            session,
            top_n=summary_top_n,
            quick_wins_limit=summary_quick_wins_limit,
        )
        top_findings = await self._fetch_top_findings(session, top_findings_limit)
        kind_severity_counts = await self._fetch_kind_severity_counts(session)
        recommendations = _build_recommendations(summary, kind_severity_counts)
        executive_summary = _build_executive_summary(summary, top_findings)
        generated_at = datetime.now(tz=UTC)
        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='engines.access_analysis.reports.deterministic_report_computed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'top_findings_returned': len(top_findings),
                    'recommendations_count': len(recommendations),
                    'total_findings': summary.total_findings,
                },
                actor_component=_COMPONENT,
                target_id='deterministic_report',
            ),
        )
        return DeterministicReport(
            summary=summary,
            top_findings=top_findings,
            recommendations=recommendations,
            executive_summary=executive_summary,
            generated_at=generated_at,
        )

    async def _fetch_top_findings(
        self,
        session: AsyncSession,
        limit: int,
    ) -> list[TopFinding]:
        """Fetch top high/critical open findings with joined evidence data."""
        severity_case = sa.case(
            (Finding.severity == 'critical', 0),
            (Finding.severity == 'high', 1),
            else_=2,
        )
        stmt = (
            sa.select(
                Finding.id,
                Finding.kind,
                Finding.severity,
                Finding.subject_id,
                Finding.account_id,
                Finding.detected_at,
                Subject.external_id.label('subject_external_id'),
                Account.username.label('account_username'),
                Account.application_id.label('account_application_id'),
                Application.code.label('application_code'),
            )
            .select_from(Finding)
            .outerjoin(Subject, Finding.subject_id == Subject.id)
            .outerjoin(Account, Finding.account_id == Account.id)
            .outerjoin(Application, Account.application_id == Application.id)
            .where(
                Finding.status == FindingStatus.open,
                Finding.severity.in_(['critical', 'high']),
            )
            .order_by(severity_case, Finding.detected_at.desc(), Finding.id.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()

        result: list[TopFinding] = []
        for row in rows:
            evidence = EvidenceSnippet(
                subject_external_id=row.subject_external_id,
                account_username=row.account_username,
                application_id=row.account_application_id,
                application_code=row.application_code,
            )
            result.append(
                TopFinding(
                    finding_id=row.id,
                    kind=row.kind.value,
                    severity=row.severity.value,
                    subject_id=row.subject_id,
                    account_id=row.account_id,
                    detected_at=row.detected_at,
                    evidence=evidence,
                )
            )
        return result

    async def _fetch_kind_severity_counts(
        self,
        session: AsyncSession,
    ) -> dict[tuple[str, str], int]:
        """Return {(kind, severity): count} for all open findings."""
        stmt = (
            sa.select(
                Finding.kind,
                Finding.severity,
                sa.func.count().label('cnt'),
            )
            .where(Finding.status == FindingStatus.open)
            .group_by(Finding.kind, Finding.severity)
        )
        rows = (await session.execute(stmt)).all()
        result: dict[tuple[str, str], int] = {}
        for row in rows:
            kind_str = row.kind.value
            sev_str = row.severity.value
            result[(kind_str, sev_str)] = row.cnt
        return result
