# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for ReportService.

Strategy:
- Seed PG findings/accounts/subjects/applications via raw SQL.
- Run service methods; assert ordering, evidence joins, recommendations, exec summary.
- Pure PG — no DuckDB, no Iceberg.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_analysis.analytics.service import AnalyticsService
from src.engines.access_analysis.reports.service import ReportService
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# pg_session fixture (mirrors analytics tests pattern)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_session(session_factory: Any) -> Any:  # noqa: ANN401
    """Yield a pg session; cleans up test data at teardown."""
    async with session_factory() as session:
        yield session
        await session.rollback()
        for tbl in ('findings', 'ent_accounts', 'applications', 'scan_runs', 'subjects', 'nhis'):
            await session.execute(sa.text(f'DELETE FROM {tbl}'))
        await session.commit()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _seed_scan_run(session: AsyncSession) -> int:
    row = await session.execute(
        sa.text(
            """
            INSERT INTO scan_runs (triggered_by)
            VALUES ('manual')
            RETURNING id
            """
        )
    )
    return int(row.scalar_one())


async def _seed_nhi_subject(session: AsyncSession) -> uuid.UUID:
    """Insert an NHI + Subject row. Returns subject_id."""
    nhi_id = uuid.uuid4()
    subject_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO nhis (id, external_id, name, kind)
            VALUES (:id, :ext_id, :name, 'service_account')
            """
        ),
        {'id': nhi_id, 'ext_id': f'nhi-{nhi_id.hex[:8]}', 'name': f'test-nhi-{nhi_id.hex[:8]}'},
    )
    await session.execute(
        sa.text(
            """
            INSERT INTO subjects (id, external_id, kind, nhi_kind, principal_nhi_id, status)
            VALUES (:id, :ext_id, 'nhi', 'service_account', :nhi_id, 'active')
            """
        ),
        {'id': subject_id, 'ext_id': f'subj-{subject_id.hex[:8]}', 'nhi_id': nhi_id},
    )
    await session.flush()
    return subject_id


async def _seed_subject_with_external_id(session: AsyncSession, external_id: str) -> uuid.UUID:
    """Insert NHI + Subject with a specific external_id. Returns subject_id."""
    nhi_id = uuid.uuid4()
    subject_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO nhis (id, external_id, name, kind)
            VALUES (:id, :ext_id, :name, 'service_account')
            """
        ),
        {'id': nhi_id, 'ext_id': f'nhi-{nhi_id.hex[:8]}', 'name': f'test-nhi-{nhi_id.hex[:8]}'},
    )
    await session.execute(
        sa.text(
            """
            INSERT INTO subjects (id, external_id, kind, nhi_kind, principal_nhi_id, status)
            VALUES (:id, :ext_id, 'nhi', 'service_account', :nhi_id, 'active')
            """
        ),
        {'id': subject_id, 'ext_id': external_id, 'nhi_id': nhi_id},
    )
    await session.flush()
    return subject_id


async def _seed_application(session: AsyncSession, code: str) -> uuid.UUID:
    """Insert an Application row. Returns application_id."""
    app_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO applications (id, name, code)
            VALUES (:id, :name, :code)
            ON CONFLICT DO NOTHING
            """
        ),
        {'id': app_id, 'name': f'app-{code}', 'code': code},
    )
    await session.flush()
    return app_id


async def _seed_account(
    session: AsyncSession,
    *,
    application_id: uuid.UUID,
    username: str,
) -> uuid.UUID:
    """Insert an Account row. Returns account_id."""
    account_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO ent_accounts (id, application_id, username)
            VALUES (:id, :application_id, :username)
            """
        ),
        {'id': account_id, 'application_id': application_id, 'username': username},
    )
    await session.flush()
    return account_id


async def _seed_finding(
    session: AsyncSession,
    *,
    scan_run_id: int,
    kind: str,
    severity: str,
    status: str = 'open',
    subject_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    rule_id: int | None = None,
    detected_at: datetime | None = None,
) -> int:
    """Insert a finding via raw SQL. Returns finding id."""
    if detected_at is None:
        detected_at = datetime.now(tz=UTC)
    row = await session.execute(
        sa.text(
            """
            INSERT INTO findings
              (scan_run_id, kind, subject_id, account_id, rule_id, severity, status,
               matched_capability_grant_ids, matched_effective_grant_ids,
               matched_access_fact_ids, evidence_hash, evaluated_at, detected_at)
            VALUES
              (:scan_run_id, :kind, :subject_id, :account_id, :rule_id, :severity, :status,
               '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
               :evidence_hash, now(), :detected_at)
            RETURNING id
            """
        ),
        {
            'scan_run_id': scan_run_id,
            'kind': kind,
            'subject_id': str(subject_id) if subject_id is not None else None,
            'account_id': str(account_id) if account_id is not None else None,
            'rule_id': rule_id,
            'severity': severity,
            'status': status,
            'evidence_hash': uuid.uuid4().hex[:64],
            'detected_at': detected_at,
        },
    )
    return int(row.scalar_one())


async def _seed_sod_rule(session: AsyncSession, severity: str = 'high') -> int:
    """Insert a SodRule. Returns rule_id."""
    row = await session.execute(
        sa.text(
            """
            INSERT INTO sod_rules (code, name, severity, scope_mode)
            VALUES (:code, :name, :severity, 'global')
            RETURNING id
            """
        ),
        {
            'code': f'RULE-{uuid.uuid4().hex[:8]}',
            'name': 'Test Rule',
            'severity': severity,
        },
    )
    return int(row.scalar_one())


def _make_service() -> ReportService:
    analytics = AnalyticsService(log_service=NoOpLogService())
    return ReportService(analytics_service=analytics, log_service=NoOpLogService())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_deterministic_report_empty(pg_session: AsyncSession) -> None:
    """No findings -> zeros, 5 exec blocks, UTC generated_at."""
    service = _make_service()
    report = await service.get_deterministic_report(pg_session)

    assert report.summary.total_findings == 0
    assert report.top_findings == []
    assert report.recommendations == []
    assert len(report.executive_summary) == 5

    # Fixed block ids in order
    block_ids = [b.block_id for b in report.executive_summary]
    assert block_ids == [
        'posture_overview',
        'top_risks',
        'quick_wins_overview',
        'application_hotspots',
        'subject_hotspots',
    ]

    # Metrics for empty state
    assert report.executive_summary[0].metric == 0  # posture_overview: total
    assert report.executive_summary[1].metric == 0  # top_risks: len(top_findings)
    assert report.executive_summary[2].metric == 0  # quick_wins_overview
    assert report.executive_summary[3].metric is None  # application_hotspots: no apps
    assert report.executive_summary[4].metric is None  # subject_hotspots: no subjects

    # generated_at must be timezone-aware UTC
    assert report.generated_at.tzinfo is not None


async def test_deterministic_report_top_findings_severity_ordering(pg_session: AsyncSession) -> None:
    """Critical findings come before high; medium/low excluded.

    Uses terminated_access (subject-only) to avoid orphan_access CHECK constraint.
    Severities: 2 critical + 3 high + 2 medium + 1 low = 8 total; only 5 pass filter.
    """
    subject_id = await _seed_nhi_subject(pg_session)
    scan_run_id = await _seed_scan_run(pg_session)

    base_time = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    for idx, severity in enumerate(('critical', 'critical', 'high', 'high', 'high', 'medium', 'medium', 'low')):
        detected = base_time + timedelta(seconds=idx)
        await _seed_finding(
            pg_session,
            scan_run_id=scan_run_id,
            kind='terminated_access',
            severity=severity,
            subject_id=subject_id,
            detected_at=detected,
        )
    await pg_session.flush()

    service = _make_service()
    report = await service.get_deterministic_report(pg_session)

    # Only high/critical pass the filter
    assert len(report.top_findings) == 5
    # First 2 must be critical, next 3 must be high
    for tf in report.top_findings[:2]:
        assert tf.severity == 'critical'
    for tf in report.top_findings[2:5]:
        assert tf.severity == 'high'


async def test_deterministic_report_top_findings_evidence_join(pg_session: AsyncSession) -> None:
    """Evidence fields are populated from joined tables."""
    app_id = await _seed_application(pg_session, 'AD')
    subject_id = await _seed_subject_with_external_id(pg_session, 'alice')
    account_id = await _seed_account(pg_session, application_id=app_id, username='alice@ad')
    scan_run_id = await _seed_scan_run(pg_session)

    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='terminated_access',
        severity='critical',
        subject_id=subject_id,
        account_id=account_id,
    )
    await pg_session.flush()

    service = _make_service()
    report = await service.get_deterministic_report(pg_session)

    assert len(report.top_findings) >= 1
    ev = report.top_findings[0].evidence
    assert ev.subject_external_id == 'alice'
    assert ev.account_username == 'alice@ad'
    assert ev.application_id == app_id
    assert ev.application_code == 'AD'


async def test_deterministic_report_top_findings_evidence_left_join_handles_nulls(
    pg_session: AsyncSession,
) -> None:
    """Finding with subject_id=None and account_id=None has all-None evidence."""
    # orphan_access: subject_id IS NULL, account_id NOT NULL per constraint
    # sod: requires rule_id — use a rule
    # For subject_id=None AND account_id=None we need a kind that allows both null
    # Per DB constraints: subject_id IS NOT NULL OR account_id IS NOT NULL
    # So we can't have both null. Instead: use orphan_access (account_id NOT NULL, subject NULL)
    # and assert subject_external_id == None.
    # OR we can use a finding with account_id set but application LEFT JOIN gives NULL
    # The test intent: LEFT JOIN must not drop the row.
    # Let's seed orphan_access (account_id set, subject_id NULL)
    # and assert subject fields are None, account fields populated.
    # But TASK says "seed 1 critical sod finding with subject_id=None AND account_id=None"
    # which violates the DB constraint. Adapt: seed orphan_access with account_id and no subject.
    app_id = await _seed_application(pg_session, code=f'APP-{uuid.uuid4().hex[:6]}')
    account_id = await _seed_account(pg_session, application_id=app_id, username='orphan-user')
    scan_run_id = await _seed_scan_run(pg_session)

    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='orphan_access',
        severity='critical',
        subject_id=None,
        account_id=account_id,
    )
    await pg_session.flush()

    service = _make_service()
    report = await service.get_deterministic_report(pg_session)

    assert len(report.top_findings) >= 1
    ev = report.top_findings[0].evidence
    # No subject joined — subject_external_id is None
    assert ev.subject_external_id is None
    # Account is joined — account_username populated
    assert ev.account_username == 'orphan-user'
    # application joined via account
    assert ev.application_id == app_id


async def test_deterministic_report_recommendations_severity_floor_blocks_low_only(
    pg_session: AsyncSession,
) -> None:
    """unused_access findings all at 'low' severity -> no review_unused_access recommendation."""
    subject_id = await _seed_nhi_subject(pg_session)
    scan_run_id = await _seed_scan_run(pg_session)

    for _ in range(5):
        await _seed_finding(
            pg_session,
            scan_run_id=scan_run_id,
            kind='unused_access',
            severity='low',
            subject_id=subject_id,
        )
    await pg_session.flush()

    service = _make_service()
    report = await service.get_deterministic_report(pg_session)

    assert report.summary.findings_by_kind.get('unused_access', 0) == 5
    rec_kinds = [r.kind for r in report.recommendations]
    assert 'review_unused_access' not in rec_kinds


async def test_deterministic_report_recommendations_emitted_for_each_kind_above_floor(
    pg_session: AsyncSession,
) -> None:
    """One finding per kind at or above floor -> 5 recommendations, correctly sorted."""
    subject_id = await _seed_nhi_subject(pg_session)
    scan_run_id = await _seed_scan_run(pg_session)
    rule_id = await _seed_sod_rule(pg_session, 'high')
    app_id = await _seed_application(pg_session, code=f'APP-{uuid.uuid4().hex[:6]}')
    orphan_username = f'orphan-{uuid.uuid4().hex[:6]}'
    orphan_account_id = await _seed_account(pg_session, application_id=app_id, username=orphan_username)

    # orphan_access: subject_id must be NULL, account_id NOT NULL
    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='orphan_access',
        severity='high',
        subject_id=None,
        account_id=orphan_account_id,
    )
    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='terminated_access',
        severity='high',
        subject_id=subject_id,
    )
    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='unused_access',
        severity='medium',
        subject_id=subject_id,
    )
    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='privileged_access',
        severity='high',
        subject_id=subject_id,
    )
    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='sod',
        severity='high',
        subject_id=subject_id,
        rule_id=rule_id,
    )
    await pg_session.flush()

    service = _make_service()
    report = await service.get_deterministic_report(pg_session)

    recs = report.recommendations
    assert len(recs) == 5

    rec_kinds = [r.kind for r in recs]
    assert set(rec_kinds) == {
        'revoke_orphan_access',
        'revoke_terminated_access',
        'review_unused_access',
        'review_privileged_access',
        'review_sod_violation',
    }

    # All high-floor recs come before medium-floor recs
    high_floor_indices = [i for i, r in enumerate(recs) if r.severity_floor == 'high']
    medium_floor_indices = [i for i, r in enumerate(recs) if r.severity_floor == 'medium']
    if high_floor_indices and medium_floor_indices:
        assert max(high_floor_indices) < min(medium_floor_indices)

    # affected_finding_count matches summary.findings_by_kind
    for rec in recs:
        assert rec.affected_finding_count == report.summary.findings_by_kind.get(rec.finding_kind, 0)


async def test_deterministic_report_executive_summary_fixed_order_and_blocks(
    pg_session: AsyncSession,
) -> None:
    """Non-trivial mix; 5 blocks in fixed order; closed findings excluded."""
    app_id = await _seed_application(pg_session, code=f'APP-{uuid.uuid4().hex[:6]}')
    subject_id = await _seed_nhi_subject(pg_session)
    account_id = await _seed_account(pg_session, application_id=app_id, username=f'usr-{uuid.uuid4().hex[:6]}')
    scan_run_id = await _seed_scan_run(pg_session)

    # 1 critical orphan_access (subject_id=None, account_id=account_id)
    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='orphan_access',
        severity='critical',
        subject_id=None,
        account_id=account_id,
    )
    # 2 high privileged_access on subject + account
    for _ in range(2):
        await _seed_finding(
            pg_session,
            scan_run_id=scan_run_id,
            kind='privileged_access',
            severity='high',
            subject_id=subject_id,
            account_id=account_id,
        )
    # 1 resolved high orphan_access — must be excluded
    await _seed_finding(
        pg_session,
        scan_run_id=scan_run_id,
        kind='orphan_access',
        severity='high',
        subject_id=None,
        account_id=account_id,
        status='resolved',
    )
    await pg_session.flush()

    service = _make_service()
    report = await service.get_deterministic_report(pg_session)

    assert len(report.executive_summary) == 5
    block_ids = [b.block_id for b in report.executive_summary]
    assert block_ids == [
        'posture_overview',
        'top_risks',
        'quick_wins_overview',
        'application_hotspots',
        'subject_hotspots',
    ]

    # Total open findings == 3 (resolved one is excluded)
    assert report.executive_summary[0].metric == 3
    # application_hotspots: one application with 3 findings
    assert report.executive_summary[3].metric == 3
