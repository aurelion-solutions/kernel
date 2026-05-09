# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AnalyticsService — read-only DuckDB analytics over access_facts + findings.

Query strategy:
- Iceberg ``normalized.access_facts`` via ``iceberg_scan(...)`` (DuckDB).
- PG ``findings`` via ``kernel_pg.findings`` (ATTACH'd at bootstrap in
  ``LakeSessionFactory._bootstrap``). Avoids duplicating PG data into the lake.

Risk score formula (MVP, non-canonical):
  risk_score = Σ(SEVERITY_WEIGHT × open_findings_count_per_severity)

All blocking DuckDB calls are offloaded to a thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_analysis.analytics.schemas import (
    SEVERITY_WEIGHT_CRITICAL,
    SEVERITY_WEIGHT_HIGH,
    SEVERITY_WEIGHT_LOW,
    SEVERITY_WEIGHT_MEDIUM,
    FindingsSummary,
    QuickWinFinding,
    RiskByApplicationItem,
    RiskByApplicationResponse,
    TopApplicationFindingCount,
    TopRiskItem,
    TopRisksResponse,
    TopSubjectFindingCount,
)
from src.inventory.accounts.models import Account
from src.inventory.assessment.findings.models import Finding, FindingKind, FindingStatus
from src.inventory.policy.sod_rules.models import SodSeverity
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_log_participant_fields

_COMPONENT = 'engines.access_analysis.analytics'

# Finding kinds eligible for quick-win recommendations (Phase 37).
# If a new kind is added later, update this set and the CASE severity expression.
_findings_summary_quick_win_kinds: frozenset[FindingKind] = frozenset(
    {
        FindingKind.orphan_access,
        FindingKind.terminated_access,
        FindingKind.unused_access,
    }
)


# ---------------------------------------------------------------------------
# SQL helpers (blocking — called via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _run_top_risks(
    lake_session: Any,
    *,
    warehouse_path: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Execute top-risks aggregation in DuckDB. Blocking."""
    sql = f"""
        WITH open_findings AS (
            SELECT
                f.subject_id,
                f.severity,
                COUNT(*) AS cnt
            FROM kernel_pg.findings f
            WHERE f.status = 'open'
              AND f.subject_id IS NOT NULL
            GROUP BY f.subject_id, f.severity
        ),
        subject_app AS (
            SELECT DISTINCT
                CAST(af.subject_id AS VARCHAR) AS subject_id,
                CAST(af.application_id_denorm AS VARCHAR) AS application_id
            FROM iceberg_scan('{warehouse_path}') af
            WHERE af.subject_id IS NOT NULL
              AND af.application_id_denorm IS NOT NULL
        ),
        scored AS (
            SELECT
                CAST(sa.subject_id AS VARCHAR) AS subject_id,
                CAST(sa.application_id AS VARCHAR) AS application_id,
                SUM(
                    CASE of.severity
                        WHEN 'critical' THEN {SEVERITY_WEIGHT_CRITICAL}
                        WHEN 'high'     THEN {SEVERITY_WEIGHT_HIGH}
                        WHEN 'medium'   THEN {SEVERITY_WEIGHT_MEDIUM}
                        WHEN 'low'      THEN {SEVERITY_WEIGHT_LOW}
                        ELSE 0
                    END * of.cnt
                ) AS risk_score,
                SUM(of.cnt) AS open_findings_count,
                SUM(CASE WHEN of.severity = 'critical'      THEN of.cnt ELSE 0 END) AS cnt_critical,
                SUM(CASE WHEN of.severity = 'high'          THEN of.cnt ELSE 0 END) AS cnt_high,
                SUM(CASE WHEN of.severity = 'medium'        THEN of.cnt ELSE 0 END) AS cnt_medium,
                SUM(CASE WHEN of.severity = 'low'           THEN of.cnt ELSE 0 END) AS cnt_low,
                SUM(CASE WHEN of.severity = 'informational' THEN of.cnt ELSE 0 END) AS cnt_informational
            FROM subject_app sa
            JOIN open_findings of ON CAST(of.subject_id AS VARCHAR) = sa.subject_id
            GROUP BY sa.subject_id, sa.application_id
        )
        SELECT
            subject_id,
            application_id,
            CAST(risk_score AS BIGINT) AS risk_score,
            CAST(open_findings_count AS BIGINT) AS open_findings_count,
            cnt_critical,
            cnt_high,
            cnt_medium,
            cnt_low,
            cnt_informational
        FROM scored
        ORDER BY risk_score DESC, subject_id ASC, application_id ASC
        LIMIT {limit}
    """
    lake_session.execute(sql)
    rows = lake_session.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                'subject_id': row[0],
                'application_id': row[1],
                'risk_score': int(row[2]) if row[2] is not None else 0,
                'open_findings_count': int(row[3]) if row[3] is not None else 0,
                'cnt_critical': int(row[4]) if row[4] is not None else 0,
                'cnt_high': int(row[5]) if row[5] is not None else 0,
                'cnt_medium': int(row[6]) if row[6] is not None else 0,
                'cnt_low': int(row[7]) if row[7] is not None else 0,
                'cnt_informational': int(row[8]) if row[8] is not None else 0,
            }
        )
    return result


def _run_risk_by_application(
    lake_session: Any,
    *,
    warehouse_path: str,
) -> list[dict[str, Any]]:
    """Execute risk-by-application aggregation in DuckDB. Blocking."""
    sql = f"""
        WITH open_findings AS (
            SELECT
                f.subject_id,
                f.severity,
                COUNT(*) AS cnt
            FROM kernel_pg.findings f
            WHERE f.status = 'open'
              AND f.subject_id IS NOT NULL
            GROUP BY f.subject_id, f.severity
        ),
        subject_app AS (
            SELECT DISTINCT
                CAST(af.subject_id AS VARCHAR) AS subject_id,
                CAST(af.application_id_denorm AS VARCHAR) AS application_id
            FROM iceberg_scan('{warehouse_path}') af
            WHERE af.subject_id IS NOT NULL
              AND af.application_id_denorm IS NOT NULL
        ),
        scored AS (
            SELECT
                CAST(sa.application_id AS VARCHAR) AS application_id,
                SUM(
                    CASE of.severity
                        WHEN 'critical' THEN {SEVERITY_WEIGHT_CRITICAL}
                        WHEN 'high'     THEN {SEVERITY_WEIGHT_HIGH}
                        WHEN 'medium'   THEN {SEVERITY_WEIGHT_MEDIUM}
                        WHEN 'low'      THEN {SEVERITY_WEIGHT_LOW}
                        ELSE 0
                    END * of.cnt
                ) AS risk_score,
                SUM(of.cnt) AS open_findings_count,
                SUM(CASE WHEN of.severity = 'critical'      THEN of.cnt ELSE 0 END) AS cnt_critical,
                SUM(CASE WHEN of.severity = 'high'          THEN of.cnt ELSE 0 END) AS cnt_high,
                SUM(CASE WHEN of.severity = 'medium'        THEN of.cnt ELSE 0 END) AS cnt_medium,
                SUM(CASE WHEN of.severity = 'low'           THEN of.cnt ELSE 0 END) AS cnt_low,
                SUM(CASE WHEN of.severity = 'informational' THEN of.cnt ELSE 0 END) AS cnt_informational
            FROM subject_app sa
            JOIN open_findings of ON CAST(of.subject_id AS VARCHAR) = sa.subject_id
            GROUP BY sa.application_id
        )
        SELECT
            application_id,
            CAST(risk_score AS BIGINT) AS risk_score,
            CAST(open_findings_count AS BIGINT) AS open_findings_count,
            cnt_critical,
            cnt_high,
            cnt_medium,
            cnt_low,
            cnt_informational
        FROM scored
        ORDER BY risk_score DESC, application_id ASC
    """
    lake_session.execute(sql)
    rows = lake_session.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                'application_id': row[0],
                'risk_score': int(row[1]) if row[1] is not None else 0,
                'open_findings_count': int(row[2]) if row[2] is not None else 0,
                'cnt_critical': int(row[3]) if row[3] is not None else 0,
                'cnt_high': int(row[4]) if row[4] is not None else 0,
                'cnt_medium': int(row[5]) if row[5] is not None else 0,
                'cnt_low': int(row[6]) if row[6] is not None else 0,
                'cnt_informational': int(row[7]) if row[7] is not None else 0,
            }
        )
    return result


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AnalyticsService:
    """Read-only analytics over Iceberg access_facts joined with PG findings.

    Emits one INFO log per call via LogService.emit_safe.
    No events — analytics is read-only and produces no domain facts.
    """

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log: LogService | NoOpLogService = log_service if log_service is not None else NoOpLogService()

    def _warehouse_path(self, lake_session: Any) -> str:
        """Build the full Iceberg path for normalized.access_facts."""
        return lake_session.iceberg_table_path('normalized', 'access_facts')

    async def get_top_risks(
        self,
        lake_session: Any,
        *,
        limit: int = 10,
    ) -> TopRisksResponse:
        """Return top-N (subject, application) pairs by risk score.

        Risk score = Σ(severity_weight × open_findings_count_per_severity).
        Sorted: risk_score DESC, then (subject_id, application_id) ASC as tie-breaker.
        """
        warehouse_path = self._warehouse_path(lake_session)
        rows = await asyncio.to_thread(
            _run_top_risks,
            lake_session,
            warehouse_path=warehouse_path,
            limit=limit,
        )
        items = [
            TopRiskItem(
                subject_id=uuid.UUID(r['subject_id']),
                application_id=uuid.UUID(r['application_id']),
                risk_score=r['risk_score'],
                open_findings_count=r['open_findings_count'],
                severity_breakdown={
                    'critical': r['cnt_critical'],
                    'high': r['cnt_high'],
                    'medium': r['cnt_medium'],
                    'low': r['cnt_low'],
                    'informational': r['cnt_informational'],
                },
            )
            for r in rows
        ]
        generated_at = datetime.now(tz=UTC)
        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='engines.access_analysis.analytics.top_risks_computed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'limit': limit, 'result_count': len(items)},
                actor_component=_COMPONENT,
                target_id='top_risks',
            ),
        )
        return TopRisksResponse(items=items, generated_at=generated_at)

    async def get_risk_by_application(
        self,
        lake_session: Any,
    ) -> RiskByApplicationResponse:
        """Return risk aggregated per application.

        Sorted: risk_score DESC, then application_id ASC as tie-breaker.
        """
        warehouse_path = self._warehouse_path(lake_session)
        rows = await asyncio.to_thread(
            _run_risk_by_application,
            lake_session,
            warehouse_path=warehouse_path,
        )
        items = [
            RiskByApplicationItem(
                application_id=uuid.UUID(r['application_id']),
                risk_score=r['risk_score'],
                open_findings_count=r['open_findings_count'],
                severity_breakdown={
                    'critical': r['cnt_critical'],
                    'high': r['cnt_high'],
                    'medium': r['cnt_medium'],
                    'low': r['cnt_low'],
                    'informational': r['cnt_informational'],
                },
            )
            for r in rows
        ]
        generated_at = datetime.now(tz=UTC)
        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='engines.access_analysis.analytics.risk_by_application_computed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'result_count': len(items)},
                actor_component=_COMPONENT,
                target_id='risk_by_application',
            ),
        )
        return RiskByApplicationResponse(items=items, generated_at=generated_at)

    async def get_findings_summary(
        self,
        session: AsyncSession,
        *,
        top_n: int = 10,
        quick_wins_limit: int = 50,
    ) -> FindingsSummary:
        """PG-only summary over assessment findings. Does not use lake_session.

        Filters: Finding.status == FindingStatus.open for ALL aggregations.
        Findings with NULL account_id are excluded from top_applications
        (subject-only findings cannot be attributed to an application).
        Severity ordering in quick_wins uses a CASE expression — not enum
        declaration order — to stay self-documenting and avoid cross-slice
        coupling. If a new severity value is added, update the CASE.
        """
        open_filter = Finding.status == FindingStatus.open

        # Query 1: total count + by_severity + critical/high counts
        severity_rows = (
            await session.execute(
                sa.select(Finding.severity, sa.func.count().label('cnt')).where(open_filter).group_by(Finding.severity)
            )
        ).all()

        findings_by_severity: dict[str, int] = {s.value: 0 for s in SodSeverity}
        total_findings = 0
        for row in severity_rows:
            key = row.severity.value if hasattr(row.severity, 'value') else str(row.severity)
            findings_by_severity[key] = row.cnt
            total_findings += row.cnt
        critical_findings = findings_by_severity.get(SodSeverity.critical.value, 0)
        high_findings = findings_by_severity.get(SodSeverity.high.value, 0)

        # Query 2: by_kind
        kind_rows = (
            await session.execute(
                sa.select(Finding.kind, sa.func.count().label('cnt')).where(open_filter).group_by(Finding.kind)
            )
        ).all()

        findings_by_kind: dict[str, int] = {k.value: 0 for k in FindingKind}
        for row in kind_rows:
            key = row.kind.value if hasattr(row.kind, 'value') else str(row.kind)
            findings_by_kind[key] = row.cnt

        # Query 3: top_applications — JOIN findings → ent_accounts, GROUP BY application_id.
        # Findings with NULL account_id are excluded by the INNER JOIN.
        app_rows = (
            await session.execute(
                sa.select(
                    Account.application_id,
                    sa.func.count().label('cnt'),
                )
                .select_from(Finding)
                .join(Account, Finding.account_id == Account.id)
                .where(open_filter)
                .group_by(Account.application_id)
                .order_by(sa.desc('cnt'), Account.application_id.asc())
                .limit(top_n)
            )
        ).all()

        top_applications = [
            TopApplicationFindingCount(application_id=row.application_id, finding_count=row.cnt) for row in app_rows
        ]

        # Query 4: top_subjects — GROUP BY subject_id (NOT NULL only)
        subj_rows = (
            await session.execute(
                sa.select(
                    Finding.subject_id,
                    sa.func.count().label('cnt'),
                )
                .where(open_filter, Finding.subject_id.is_not(None))
                .group_by(Finding.subject_id)
                .order_by(sa.desc('cnt'), Finding.subject_id.asc())
                .limit(top_n)
            )
        ).all()

        top_subjects = [TopSubjectFindingCount(subject_id=row.subject_id, finding_count=row.cnt) for row in subj_rows]

        # Query 5: quick_wins — high/critical findings in quick-win kinds.
        # Severity ordering: critical=0, high=1 via CASE (not enum declaration order).
        quick_win_kind_values = [k.value for k in _findings_summary_quick_win_kinds]
        severity_case = sa.case(
            (Finding.severity == SodSeverity.critical, 0),
            (Finding.severity == SodSeverity.high, 1),
            else_=2,
        )
        qw_rows = (
            await session.execute(
                sa.select(
                    Finding.id,
                    Finding.kind,
                    Finding.severity,
                    Finding.subject_id,
                    Finding.account_id,
                    Finding.detected_at,
                )
                .where(
                    open_filter,
                    Finding.kind.in_(quick_win_kind_values),
                    Finding.severity.in_([SodSeverity.high.value, SodSeverity.critical.value]),
                )
                .order_by(severity_case, Finding.detected_at.desc(), Finding.id.desc())
                .limit(quick_wins_limit)
            )
        ).all()

        quick_wins = [
            QuickWinFinding(
                finding_id=row.id,
                kind=row.kind.value if hasattr(row.kind, 'value') else str(row.kind),
                severity=row.severity.value if hasattr(row.severity, 'value') else str(row.severity),
                subject_id=row.subject_id,
                account_id=row.account_id,
                detected_at=row.detected_at,
            )
            for row in qw_rows
        ]

        generated_at = datetime.now(tz=UTC)
        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='engines.access_analysis.analytics.findings_summary_computed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'top_n': top_n,
                    'total_findings': total_findings,
                    'quick_wins_count': len(quick_wins),
                },
                actor_component=_COMPONENT,
                target_id='findings_summary',
            ),
        )
        return FindingsSummary(
            total_findings=total_findings,
            findings_by_severity=findings_by_severity,
            findings_by_kind=findings_by_kind,
            critical_findings=critical_findings,
            high_findings=high_findings,
            top_applications=top_applications,
            top_subjects=top_subjects,
            quick_wins=quick_wins,
            generated_at=generated_at,
        )
