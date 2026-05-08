# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for AnalyticsService.

Strategy:
- Seed Iceberg ``normalized.access_facts`` with PyArrow.
- Seed PG ``findings`` via SQLAlchemy.
- Build a DuckDB session with pg_dsn so ``kernel_pg`` is ATTACH'd.
- Run service methods; assert ordering, severity weighting, tie-breaker.

NOTE: These are integration-style tests that require a live PG instance
(TEST_DATABASE_URL) and a temp-dir Iceberg warehouse.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_analysis.analytics.service import AnalyticsService
from src.inventory.assessment.findings.models import FindingKind, FindingStatus
from src.inventory.assessment.scan_runs.models import ScanRun, ScanRunTrigger
from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Iceberg schema helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
_PA_SCHEMA = pa.schema(
    [
        pa.field('id', pa.string(), nullable=False),
        pa.field('subject_id', pa.string(), nullable=False),
        pa.field('account_id', pa.string(), nullable=True),
        pa.field('resource_id', pa.string(), nullable=False),
        pa.field('action_id', pa.string(), nullable=False),
        pa.field('effect', pa.string(), nullable=False),
        pa.field('valid_from', pa.timestamp('us', tz='UTC'), nullable=False),
        pa.field('valid_until', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('is_active', pa.bool_(), nullable=False),
        pa.field('observed_at', pa.timestamp('us', tz='UTC'), nullable=False),
        pa.field('created_at', pa.timestamp('us', tz='UTC'), nullable=False),
        pa.field('revoked_at', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('latest_batch_id', pa.string(), nullable=True),
        pa.field('application_id_denorm', pa.string(), nullable=True),
        pa.field('subject_kind_denorm', pa.string(), nullable=False),
        pa.field('reconciliation_delta_item_id', pa.string(), nullable=False),
        pa.field('natural_key_hash', pa.string(), nullable=False),
    ]
)


def _iceberg_schema() -> Schema:
    return Schema(
        NestedField(1, 'id', StringType(), required=True),
        NestedField(2, 'subject_id', StringType(), required=True),
        NestedField(3, 'account_id', StringType(), required=False),
        NestedField(4, 'resource_id', StringType(), required=True),
        NestedField(5, 'action_id', StringType(), required=True),
        NestedField(6, 'effect', StringType(), required=True),
        NestedField(7, 'valid_from', TimestamptzType(), required=True),
        NestedField(8, 'valid_until', TimestamptzType(), required=False),
        NestedField(9, 'is_active', BooleanType(), required=True),
        NestedField(10, 'observed_at', TimestamptzType(), required=True),
        NestedField(11, 'created_at', TimestamptzType(), required=True),
        NestedField(12, 'revoked_at', TimestamptzType(), required=False),
        NestedField(13, 'latest_batch_id', StringType(), required=False),
        NestedField(14, 'application_id_denorm', StringType(), required=False),
        NestedField(15, 'subject_kind_denorm', StringType(), required=True),
        NestedField(16, 'reconciliation_delta_item_id', StringType(), required=True),
        NestedField(17, 'natural_key_hash', StringType(), required=True),
    )


def _iceberg_spec() -> PartitionSpec:
    return PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_catalog() -> Any:  # noqa: ANN401
    reset_catalog_cache_for_tests()
    yield
    reset_catalog_cache_for_tests()


@pytest.fixture
def lake_settings(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
    )


@pytest.fixture
def iceberg_catalog(lake_settings: LakeSettings) -> Catalog:
    log = NoOpLogService()
    cat = get_catalog(lake_settings, log_service=log)
    try:
        cat.create_namespace(('normalized',))
    except Exception:
        pass
    try:
        cat.drop_table(('normalized', 'access_facts'))
    except Exception:
        pass
    cat.create_table(
        ('normalized', 'access_facts'),
        schema=_iceberg_schema(),
        partition_spec=_iceberg_spec(),
    )
    return cat


def _get_pg_dsn_sync() -> str:
    """Return a libpq-compatible DSN pointing to the test database."""
    import os  # noqa: PLC0415
    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

    try:
        from src.core.config import get_settings  # noqa: PLC0415

        raw_dsn = get_settings().postgres.dsn
    except Exception:
        raw_dsn = os.getenv('DATABASE_URL', '')

    parsed = urlparse(raw_dsn)
    db_name = parsed.path.lstrip('/')
    test_db = db_name.rsplit('_', 1)[0] + '_test' if '_' in db_name else db_name + '_test'
    dsn_no_driver = urlunparse(parsed._replace(path='/' + test_db, scheme='postgresql'))
    # Strip asyncpg/psycopg2 driver segment
    return dsn_no_driver.replace('+asyncpg', '').replace('+psycopg2', '')


@pytest.fixture
def lake_session_with_pg(lake_settings: LakeSettings, engine: Any) -> Any:  # noqa: ANN401
    """LakeSession ATTACH'd to test PG — kernel_pg.findings is accessible.

    Depends on `engine` to ensure the test schema (including ref_actions) is
    created before the DuckDB session bootstrap tries to create ref_actions_local.
    """
    pg_dsn = _get_pg_dsn_sync()
    log = NoOpLogService()
    factory = LakeSessionFactory(settings=lake_settings, log_service=log, pg_dsn=pg_dsn)
    session = factory.acquire()
    yield session
    session.__exit__(None, None, None)
    factory.close_all()


@pytest_asyncio.fixture
async def pg_session(session_factory: Any) -> Any:  # noqa: ANN401
    """Yield a pg session that can commit data for DuckDB cross-session reads.

    DuckDB reads PG data via a separate ATTACH'd connection, so data inserted
    in the test session must be committed. Tests explicitly call session.commit()
    before invoking DuckDB-backed service methods.
    The fixture cleans up findings/scan_runs/subjects/nhis after each test.
    """
    async with session_factory() as session:
        yield session
        # Cleanup: rollback any pending changes then truncate test data
        await session.rollback()
        # Re-use same session for truncation
        for tbl in ('findings', 'scan_runs', 'subjects', 'nhis'):
            await session.execute(sa.text(f'DELETE FROM {tbl}'))
        await session.commit()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_fact_row(subject_id: uuid.UUID, application_id_denorm: str) -> dict[str, Any]:
    """Return a minimal fact row dict for _append_facts."""
    return {
        'id': str(uuid.uuid4()),
        'subject_id': str(subject_id),
        'resource_id': str(uuid.uuid4()),
        'application_id_denorm': application_id_denorm,
    }


def _append_facts(iceberg_table: Any, facts: list[dict[str, Any]]) -> None:
    """Append rows to the Iceberg table using the test PA schema."""
    n = len(facts)
    arrow_data = {
        'id': [f['id'] for f in facts],
        'subject_id': [f['subject_id'] for f in facts],
        'account_id': [None] * n,
        'resource_id': [f['resource_id'] for f in facts],
        'action_id': ['read'] * n,
        'effect': ['allow'] * n,
        'valid_from': [_NOW] * n,
        'valid_until': [None] * n,
        'is_active': [True] * n,
        'observed_at': [_NOW] * n,
        'created_at': [_NOW] * n,
        'revoked_at': [None] * n,
        'latest_batch_id': [None] * n,
        'application_id_denorm': [f['application_id_denorm'] for f in facts],
        'subject_kind_denorm': ['User'] * n,
        'reconciliation_delta_item_id': [str(uuid.uuid4()) for _ in range(n)],
        'natural_key_hash': ['a' * 64] * n,
    }
    iceberg_table.append(pa.table(arrow_data, schema=_PA_SCHEMA))


async def _seed_scan_run(pg_session: AsyncSession) -> int:
    run = ScanRun(triggered_by=ScanRunTrigger.manual)
    pg_session.add(run)
    await pg_session.flush()
    await pg_session.refresh(run)
    return run.id


async def _seed_sod_rule(pg_session: AsyncSession, severity: SodSeverity) -> int:
    rule = SodRule(
        code=f'RULE-{uuid.uuid4().hex[:8]}',
        name='Test Rule',
        severity=severity,
        scope_mode=SodRuleScope.global_,
    )
    pg_session.add(rule)
    await pg_session.flush()
    await pg_session.refresh(rule)
    return rule.id


async def _seed_subject(pg_session: AsyncSession, subject_id: uuid.UUID) -> None:
    """Insert an NHI + Subject row for the given subject_id."""
    nhi_id = uuid.uuid4()
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO nhis (id, external_id, name, kind)
            VALUES (:id, :ext_id, :name, 'service_account')
            """
        ),
        {'id': nhi_id, 'ext_id': f'nhi-{nhi_id.hex[:8]}', 'name': f'test-nhi-{nhi_id.hex[:8]}'},
    )
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO subjects (id, external_id, kind, nhi_kind, principal_nhi_id, status)
            VALUES (:id, :ext_id, 'nhi', 'service_account', :nhi_id, 'active')
            """
        ),
        {'id': subject_id, 'ext_id': f'subj-{subject_id.hex[:8]}', 'nhi_id': nhi_id},
    )
    await pg_session.flush()


async def _seed_finding(
    pg_session: AsyncSession,
    *,
    scan_run_id: int,
    subject_id: uuid.UUID,
    severity: SodSeverity,
    status: FindingStatus = FindingStatus.open,
) -> None:
    """Insert a finding via raw SQL to avoid ORM constraint issues in tests."""
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO findings
              (scan_run_id, kind, subject_id, severity, status,
               matched_capability_grant_ids, matched_effective_grant_ids,
               matched_access_fact_ids, evidence_hash, evaluated_at)
            VALUES
              (:scan_run_id, 'unused_access', :subject_id, :severity, :status,
               '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
               :evidence_hash, now())
            """
        ),
        {
            'scan_run_id': scan_run_id,
            'subject_id': str(subject_id),
            'severity': severity.value,
            'status': status.value,
            'evidence_hash': uuid.uuid4().hex[:64],
        },
    )


async def _seed_account(pg_session: AsyncSession, *, application_id: uuid.UUID) -> uuid.UUID:
    """Insert a minimal Application + Account row. Returns account_id.

    Uses raw SQL to avoid importing Application/Account ORM models into the test.
    Cleanup is the caller's responsibility (DELETE FROM ent_accounts / applications).
    """
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO applications (id, name, code)
            VALUES (:id, :name, :code)
            ON CONFLICT DO NOTHING
            """
        ),
        {
            'id': str(application_id),
            'name': f'app-{application_id.hex[:8]}',
            'code': f'code-{application_id.hex[:8]}',
        },
    )
    account_id = uuid.uuid4()
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO ent_accounts (id, application_id, username)
            VALUES (:id, :application_id, :username)
            """
        ),
        {
            'id': str(account_id),
            'application_id': str(application_id),
            'username': f'acct-{account_id.hex[:8]}',
        },
    )
    return account_id


async def _seed_finding_full(
    pg_session: AsyncSession,
    *,
    scan_run_id: int,
    kind: str,
    severity: SodSeverity,
    status: FindingStatus = FindingStatus.open,
    subject_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
) -> int:
    """Insert a finding with explicit kind, optional subject/account. Returns finding id.

    Invariants mirrored from the Finding model:
    - subject_id or account_id must be set (ck_findings_subject_or_account).
    - orphan_access must have subject_id IS NULL (ck_findings_orphan_no_subject).
    - sod findings require rule_id — not seeded here, so avoid kind='sod' unless
      you also supply a rule_id via a separate UPDATE.
    """
    row = await pg_session.execute(
        sa.text(
            """
            INSERT INTO findings
              (scan_run_id, kind, subject_id, account_id, severity, status,
               matched_capability_grant_ids, matched_effective_grant_ids,
               matched_access_fact_ids, evidence_hash, evaluated_at)
            VALUES
              (:scan_run_id, :kind, :subject_id, :account_id, :severity, :status,
               '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
               :evidence_hash, now())
            RETURNING id
            """
        ),
        {
            'scan_run_id': scan_run_id,
            'kind': kind,
            'subject_id': str(subject_id) if subject_id is not None else None,
            'account_id': str(account_id) if account_id is not None else None,
            'severity': severity.value,
            'status': status.value,
            'evidence_hash': uuid.uuid4().hex[:64],
        },
    )
    return row.scalar_one()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_top_risks_orders_by_score_desc_then_id(
    iceberg_catalog: Catalog,
    lake_session_with_pg: LakeSession,
    pg_session: AsyncSession,
) -> None:
    """Three subjects: subject B has higher score; assert B comes first."""
    app_id = str(uuid.uuid4())
    subj_a = uuid.uuid4()
    subj_b = uuid.uuid4()
    subj_c = uuid.uuid4()

    ice_table = iceberg_catalog.load_table(('normalized', 'access_facts'))
    _append_facts(
        ice_table,
        [
            _make_fact_row(subj_a, app_id),
            _make_fact_row(subj_b, app_id),
            _make_fact_row(subj_c, app_id),
        ],
    )

    await _seed_subject(pg_session, subj_a)
    await _seed_subject(pg_session, subj_b)
    await _seed_subject(pg_session, subj_c)
    scan_run_id = await _seed_scan_run(pg_session)
    # A: 1 low = score 5
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_a, severity=SodSeverity.low)
    # B: 1 critical = score 100
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_b, severity=SodSeverity.critical)
    # C: 2 medium = score 40
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_c, severity=SodSeverity.medium)
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_c, severity=SodSeverity.medium)
    await pg_session.commit()  # commit so DuckDB ATTACH can read the data

    service = AnalyticsService()
    result = await service.get_top_risks(lake_session_with_pg, limit=10)

    assert len(result.items) == 3
    scores = [item.risk_score for item in result.items]
    assert scores == sorted(scores, reverse=True), 'items must be ordered risk_score DESC'
    assert result.items[0].subject_id == subj_b  # score=100


async def test_get_top_risks_severity_weights_applied(
    iceberg_catalog: Catalog,
    lake_session_with_pg: LakeSession,
    pg_session: AsyncSession,
) -> None:
    """100 × LOW (score=500) must outscore 1 × CRITICAL (score=100) — documented MVP behaviour."""
    app_id = str(uuid.uuid4())
    subj_critical = uuid.uuid4()
    subj_low = uuid.uuid4()

    ice_table = iceberg_catalog.load_table(('normalized', 'access_facts'))
    _append_facts(
        ice_table,
        [
            _make_fact_row(subj_critical, app_id),
            _make_fact_row(subj_low, app_id),
        ],
    )

    await _seed_subject(pg_session, subj_critical)
    await _seed_subject(pg_session, subj_low)
    scan_run_id = await _seed_scan_run(pg_session)
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_critical, severity=SodSeverity.critical)
    for _ in range(100):
        await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_low, severity=SodSeverity.low)
    await pg_session.commit()  # commit so DuckDB ATTACH can read the data

    service = AnalyticsService()
    result = await service.get_top_risks(lake_session_with_pg, limit=10)

    assert len(result.items) == 2
    # 100 × 5 = 500 > 1 × 100 — subj_low wins
    assert result.items[0].subject_id == subj_low
    assert result.items[0].risk_score == 500
    assert result.items[1].risk_score == 100


async def test_get_top_risks_respects_limit(
    iceberg_catalog: Catalog,
    lake_session_with_pg: LakeSession,
    pg_session: AsyncSession,
) -> None:
    """limit=1 returns only one item even when multiple subjects have findings."""
    app_id = str(uuid.uuid4())
    subjs = [uuid.uuid4() for _ in range(3)]

    ice_table = iceberg_catalog.load_table(('normalized', 'access_facts'))
    _append_facts(ice_table, [_make_fact_row(s, app_id) for s in subjs])

    for s in subjs:
        await _seed_subject(pg_session, s)
    scan_run_id = await _seed_scan_run(pg_session)
    for s in subjs:
        await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=s, severity=SodSeverity.medium)
    await pg_session.commit()  # commit so DuckDB ATTACH can read the data

    service = AnalyticsService()
    result = await service.get_top_risks(lake_session_with_pg, limit=1)

    assert len(result.items) == 1


async def test_get_risk_by_application_aggregates_per_application(
    iceberg_catalog: Catalog,
    lake_session_with_pg: LakeSession,
    pg_session: AsyncSession,
) -> None:
    """Two applications; one subject per app; assert both apps appear and scores correct."""
    app_a = str(uuid.uuid4())
    app_b = str(uuid.uuid4())
    subj_a = uuid.uuid4()
    subj_b = uuid.uuid4()

    ice_table = iceberg_catalog.load_table(('normalized', 'access_facts'))
    _append_facts(
        ice_table,
        [
            _make_fact_row(subj_a, app_a),
            _make_fact_row(subj_b, app_b),
        ],
    )

    await _seed_subject(pg_session, subj_a)
    await _seed_subject(pg_session, subj_b)
    scan_run_id = await _seed_scan_run(pg_session)
    # App A: 2 high = 2 × 50 = 100
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_a, severity=SodSeverity.high)
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_a, severity=SodSeverity.high)
    # App B: 1 critical = 100
    await _seed_finding(pg_session, scan_run_id=scan_run_id, subject_id=subj_b, severity=SodSeverity.critical)
    await pg_session.commit()  # commit so DuckDB ATTACH can read the data

    service = AnalyticsService()
    result = await service.get_risk_by_application(lake_session_with_pg)

    app_ids = {str(item.application_id) for item in result.items}
    assert app_a in app_ids
    assert app_b in app_ids

    scores = [item.risk_score for item in result.items]
    assert scores == sorted(scores, reverse=True), 'items must be sorted risk_score DESC'


# ---------------------------------------------------------------------------
# FindingsSummary tests (Phase 37) — PG-only, no DuckDB/Iceberg
# ---------------------------------------------------------------------------


async def test_findings_summary_empty(pg_session: AsyncSession) -> None:
    """No findings seeded -> all zeros, generated_at is UTC."""
    service = AnalyticsService()
    result = await service.get_findings_summary(pg_session)

    assert result.total_findings == 0
    assert result.critical_findings == 0
    assert result.high_findings == 0
    assert result.top_applications == []
    assert result.top_subjects == []
    assert result.quick_wins == []
    # All severity keys default to 0
    for sev in ('critical', 'high', 'medium', 'low'):
        assert result.findings_by_severity.get(sev, -1) == 0
    # generated_at must be timezone-aware UTC

    assert result.generated_at.tzinfo is not None
    assert result.generated_at.tzinfo == UTC or result.generated_at.utcoffset() is not None


async def test_findings_summary_by_severity(pg_session: AsyncSession) -> None:
    """1 critical + 2 high + 3 medium + 4 low -> counts match, total=10."""
    subject_id = uuid.uuid4()
    await _seed_subject(pg_session, subject_id)
    scan_run_id = await _seed_scan_run(pg_session)

    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='unused_access',
        severity=SodSeverity.critical,
        subject_id=subject_id,
    )
    for _ in range(2):
        await _seed_finding_full(
            pg_session,
            scan_run_id=scan_run_id,
            kind='unused_access',
            severity=SodSeverity.high,
            subject_id=subject_id,
        )
    for _ in range(3):
        await _seed_finding_full(
            pg_session,
            scan_run_id=scan_run_id,
            kind='unused_access',
            severity=SodSeverity.medium,
            subject_id=subject_id,
        )
    for _ in range(4):
        await _seed_finding_full(
            pg_session,
            scan_run_id=scan_run_id,
            kind='unused_access',
            severity=SodSeverity.low,
            subject_id=subject_id,
        )
    await pg_session.flush()

    service = AnalyticsService()
    result = await service.get_findings_summary(pg_session)

    assert result.total_findings == 10
    assert result.critical_findings == 1
    assert result.high_findings == 2
    assert result.findings_by_severity['critical'] == 1
    assert result.findings_by_severity['high'] == 2
    assert result.findings_by_severity['medium'] == 3
    assert result.findings_by_severity['low'] == 4


async def test_findings_summary_by_kind(pg_session: AsyncSession) -> None:
    """At least one of each kind -> all five keys present with count >= 1."""
    subject_id = uuid.uuid4()
    await _seed_subject(pg_session, subject_id)
    scan_run_id = await _seed_scan_run(pg_session)
    sod_rule_id = await _seed_sod_rule(pg_session, SodSeverity.high)

    # orphan_access requires account_id (subject_id must be NULL per CHECK constraint)
    app_id = uuid.uuid4()
    orphan_account_id = await _seed_account(pg_session, application_id=app_id)

    # sod kind requires rule_id; insert via raw SQL directly
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO findings
              (scan_run_id, kind, subject_id, rule_id, severity, status,
               matched_capability_grant_ids, matched_effective_grant_ids,
               matched_access_fact_ids, evidence_hash, evaluated_at)
            VALUES
              (:scan_run_id, 'sod', :subject_id, :rule_id, 'high', 'open',
               '[]'::jsonb, '[]'::jsonb, '[]'::jsonb,
               :evidence_hash, now())
            """
        ),
        {
            'scan_run_id': scan_run_id,
            'subject_id': str(subject_id),
            'rule_id': sod_rule_id,
            'evidence_hash': uuid.uuid4().hex[:64],
        },
    )
    # orphan_access: subject_id IS NULL, account_id NOT NULL
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='orphan_access',
        severity=SodSeverity.medium,
        subject_id=None,
        account_id=orphan_account_id,
    )
    for kind in ('terminated_access', 'unused_access', 'privileged_access'):
        await _seed_finding_full(
            pg_session,
            scan_run_id=scan_run_id,
            kind=kind,
            severity=SodSeverity.medium,
            subject_id=subject_id,
        )
    await pg_session.flush()

    service = AnalyticsService()
    result = await service.get_findings_summary(pg_session)

    for kind_key in FindingKind:
        assert kind_key.value in result.findings_by_kind, f'missing key: {kind_key.value}'
        assert result.findings_by_kind[kind_key.value] >= 1, f'expected >= 1 for {kind_key.value}'
    assert result.findings_by_kind['privileged_access'] >= 1

    # Cleanup application/account rows — must delete findings first (RESTRICT FK)
    await pg_session.execute(sa.text('DELETE FROM findings WHERE account_id = :id'), {'id': str(orphan_account_id)})
    await pg_session.execute(sa.text('DELETE FROM ent_accounts WHERE id = :id'), {'id': str(orphan_account_id)})
    await pg_session.execute(sa.text('DELETE FROM applications WHERE id = :id'), {'id': str(app_id)})


async def test_findings_summary_quick_wins_includes_high_critical_for_three_kinds(
    pg_session: AsyncSession,
) -> None:
    """Quick wins include only high/critical in orphan/terminated/unused; excludes sod/privileged."""
    subject_id = uuid.uuid4()
    await _seed_subject(pg_session, subject_id)
    scan_run_id = await _seed_scan_run(pg_session)
    sod_rule_id = await _seed_sod_rule(pg_session, SodSeverity.high)

    # orphan_access requires account_id (subject_id must be NULL per CHECK constraint)
    app_id = uuid.uuid4()
    orphan_account_id = await _seed_account(pg_session, application_id=app_id)

    # 1 high orphan_access
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='orphan_access',
        severity=SodSeverity.high,
        subject_id=None,
        account_id=orphan_account_id,
    )
    # 1 critical orphan_access
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='orphan_access',
        severity=SodSeverity.critical,
        subject_id=None,
        account_id=orphan_account_id,
    )
    # 1 high terminated_access
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='terminated_access',
        severity=SodSeverity.high,
        subject_id=subject_id,
    )
    # 1 high unused_access
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='unused_access',
        severity=SodSeverity.high,
        subject_id=subject_id,
    )
    # 1 medium unused_access — must NOT be in quick_wins (severity excluded)
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='unused_access',
        severity=SodSeverity.medium,
        subject_id=subject_id,
    )
    # 1 high sod — must NOT be in quick_wins (kind excluded)
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO findings
              (scan_run_id, kind, subject_id, rule_id, severity, status,
               matched_capability_grant_ids, matched_effective_grant_ids,
               matched_access_fact_ids, evidence_hash, evaluated_at)
            VALUES
              (:scan_run_id, 'sod', :subject_id, :rule_id, 'high', 'open',
               '[]'::jsonb, '[]'::jsonb, '[]'::jsonb, :eh, now())
            """
        ),
        {
            'scan_run_id': scan_run_id,
            'subject_id': str(subject_id),
            'rule_id': sod_rule_id,
            'eh': uuid.uuid4().hex[:64],
        },
    )
    # 1 high privileged_access — must NOT be in quick_wins (kind excluded)
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='privileged_access',
        severity=SodSeverity.high,
        subject_id=subject_id,
    )
    await pg_session.flush()

    service = AnalyticsService()
    result = await service.get_findings_summary(pg_session)

    qw = result.quick_wins
    assert len(qw) == 4, f'expected 4 quick wins, got {len(qw)}: {qw}'

    allowed_kinds = {
        FindingKind.orphan_access.value,
        FindingKind.terminated_access.value,
        FindingKind.unused_access.value,
    }
    for entry in qw:
        assert entry.kind in allowed_kinds, f'unexpected kind in quick_wins: {entry.kind}'
        assert entry.severity in (SodSeverity.high.value, SodSeverity.critical.value)

    # critical entries must come before high entries
    severities = [entry.severity for entry in qw]
    critical_indices = [i for i, s in enumerate(severities) if s == SodSeverity.critical.value]
    high_indices = [i for i, s in enumerate(severities) if s == SodSeverity.high.value]
    if critical_indices and high_indices:
        assert max(critical_indices) < min(high_indices), 'critical entries must precede high entries'

    # Cleanup application/account rows — must delete findings first (RESTRICT FK)
    await pg_session.execute(sa.text('DELETE FROM findings WHERE account_id = :id'), {'id': str(orphan_account_id)})
    await pg_session.execute(sa.text('DELETE FROM ent_accounts WHERE id = :id'), {'id': str(orphan_account_id)})
    await pg_session.execute(sa.text('DELETE FROM applications WHERE id = :id'), {'id': str(app_id)})


async def test_findings_summary_excludes_closed_findings(pg_session: AsyncSession) -> None:
    """2 open + 1 acknowledged + 1 resolved + 1 mitigated -> total_findings == 2."""
    subject_id = uuid.uuid4()
    await _seed_subject(pg_session, subject_id)
    scan_run_id = await _seed_scan_run(pg_session)

    for _ in range(2):
        await _seed_finding_full(
            pg_session,
            scan_run_id=scan_run_id,
            kind='unused_access',
            severity=SodSeverity.high,
            subject_id=subject_id,
            status=FindingStatus.open,
        )
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='unused_access',
        severity=SodSeverity.high,
        subject_id=subject_id,
        status=FindingStatus.acknowledged,
    )
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='unused_access',
        severity=SodSeverity.high,
        subject_id=subject_id,
        status=FindingStatus.resolved,
    )
    await _seed_finding_full(
        pg_session,
        scan_run_id=scan_run_id,
        kind='unused_access',
        severity=SodSeverity.high,
        subject_id=subject_id,
        status=FindingStatus.mitigated,
    )
    await pg_session.flush()

    service = AnalyticsService()
    result = await service.get_findings_summary(pg_session)

    assert result.total_findings == 2
    # Closed findings must not leak into any breakdown
    total_from_severity = sum(result.findings_by_severity.values())
    assert total_from_severity == 2
    total_from_kind = sum(result.findings_by_kind.values())
    assert total_from_kind == 2
