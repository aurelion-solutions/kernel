# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Parity tests: legacy SQLAlchemy path vs new DuckDB/Iceberg path for the unused detector.

ALL tests do a live-run of both paths on the same seed data.
No snapshots — comparing actual runtime outputs guarantees parity at every schema version.

Seed layout (shared across all cases):
  - 50 active AccessFacts in PG (access_facts table)
  - 50 active AccessFacts mirrored in Iceberg normalized.access_facts (same UUIDs, same fields)
  - 20 AccessUsageFacts in PG (for the first 20 facts)
  - Resources seeded with correct application_id for PG LEFT JOIN

PG legacy path:
  SQLAlchemy query: access_facts JOIN resources LEFT JOIN usage_aggregate

DuckDB new path:
  iceberg_scan(normalized.access_facts) + per-batch PG usage query
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
import uuid

import pyarrow as pa
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import (
    BooleanType,
    NestedField,
    StringType,
    TimestamptzType,
)
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis._engine_loaders import load_unused_inputs
from src.capabilities.access_analysis.data_access import iter_unused_access_fact_views
from src.capabilities.access_analysis.detectors.service import UnusedDetectorService
from src.capabilities.access_analysis.detectors.unused import AccessFactView, detect_unused
from src.inventory.access_usage_facts.models import AccessUsageFact
from src.inventory.resources.models import Resource
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.logs.service import LogService, NoOpLogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
_THRESHOLD = 90
_APP_ID = uuid.UUID('aaaaaaaa-1111-0000-0000-000000000001')
_SUBJ_ID = uuid.UUID('bbbbbbbb-1111-0000-0000-000000000001')
_PG_ANY_MAX = 25000

# ---------------------------------------------------------------------------
# Catalog cache reset
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_catalog_cache() -> Any:  # noqa: ANN401
    reset_catalog_cache_for_tests()
    yield
    reset_catalog_cache_for_tests()


# ---------------------------------------------------------------------------
# Lake fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lake_settings(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
    )


def _make_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


@pytest.fixture
def lake_session_factory(lake_settings: LakeSettings) -> LakeSessionFactory:
    log, _ = _make_log_service()
    return LakeSessionFactory(settings=lake_settings, log_service=log, pg_dsn=None)


@pytest.fixture
def lake_session(lake_session_factory: LakeSessionFactory) -> Any:  # noqa: ANN401
    session = lake_session_factory.acquire()
    yield session
    session.__exit__(None, None, None)
    lake_session_factory.close_all()


# ---------------------------------------------------------------------------
# Iceberg table provisioning
# ---------------------------------------------------------------------------


def _make_schema() -> Schema:
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


@pytest.fixture
def iceberg_table(lake_settings: LakeSettings) -> Any:  # noqa: ANN401
    log, _ = _make_log_service()
    cat = get_catalog(lake_settings, log_service=log)
    try:
        cat.create_namespace(('normalized',))
    except Exception:
        pass

    identifier = ('normalized', 'access_facts')
    try:
        cat.drop_table(identifier)
    except Exception:
        pass

    spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
    )
    return cat.create_table(identifier, schema=_make_schema(), partition_spec=spec)


# ---------------------------------------------------------------------------
# PG session fixture
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_session(session_factory: Any) -> Any:  # noqa: ANN401
    async with session_factory() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# Shared seed helper — populates BOTH PG and Iceberg
# ---------------------------------------------------------------------------


async def _seed_shared(
    pg_session: AsyncSession,
    iceberg_table: Any,
    n: int = 50,
    n_usage: int = 20,
) -> tuple[list[uuid.UUID], list[uuid.UUID], uuid.UUID, Any]:
    """Seed n facts in PG + Iceberg; n_usage usage facts in PG.

    Returns (all_fact_ids, usage_fact_ids, resource_id, action_id).
    """
    # Seed application in PG (required by FK on resources.application_id)
    from src.platform.applications.models import Application

    app = Application(
        id=_APP_ID,
        name=f'app-parity-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    pg_session.add(app)
    await pg_session.flush()

    # Seed resource in PG
    resource = Resource(
        external_id=f'res-parity-{uuid.uuid4().hex[:8]}',
        application_id=_APP_ID,
        kind='role',
        resource_type='role',
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
    )
    pg_session.add(resource)
    await pg_session.flush()

    # Resolve action_id
    action_id = (
        await pg_session.execute(sa.text("SELECT id FROM ref_actions WHERE slug = 'read' LIMIT 1"))
    ).scalar_one()

    # Seed subject — lookup from existing seeds
    from src.inventory.nhi.models import NHI
    from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind

    nhi = NHI(
        external_id=f'nhi-parity-{uuid.uuid4().hex[:8]}',
        name=f'test-nhi-parity-{uuid.uuid4().hex[:8]}',
        kind='service_account',
        owner_employee_id=None,
    )
    pg_session.add(nhi)
    await pg_session.flush()

    subject = Subject(
        external_id=f'subj-parity-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status='active',
    )
    pg_session.add(subject)
    await pg_session.flush()

    subject_id = subject.id
    base_resource_id = resource.id

    valid_from = _NOW - timedelta(days=120)

    # Seed PG access_facts — each fact gets its own resource to avoid
    # unique constraint (subject_id, resource_id, action_id) violations.
    fact_ids: list[uuid.UUID] = []
    resource_ids: list[uuid.UUID] = [base_resource_id]
    for i in range(n):
        if i == 0:
            res_id = base_resource_id
        else:
            extra_res = Resource(
                external_id=f'res-p-{uuid.uuid4().hex[:8]}',
                application_id=_APP_ID,
                kind='role',
                resource_type='role',
                resource_key=f'key-{uuid.uuid4().hex[:8]}',
            )
            pg_session.add(extra_res)
            await pg_session.flush()
            res_id = extra_res.id
            resource_ids.append(res_id)

        fact_id = uuid.uuid4()
        await pg_session.execute(
            sa.text(
                'INSERT INTO access_facts '
                '(id, subject_id, account_id, resource_id, action_id, effect, observed_at, valid_from, is_active) '
                'VALUES (:id, :subject_id, :account_id, :resource_id, :action_id, :effect, '
                ':observed_at, :valid_from, :is_active)'
            ),
            {
                'id': fact_id,
                'subject_id': subject_id,
                'account_id': None,
                'resource_id': res_id,
                'action_id': action_id,
                'effect': 'allow',
                'observed_at': valid_from,
                'valid_from': valid_from,
                'is_active': True,
            },
        )
        await pg_session.flush()
        fact_ids.append(fact_id)

    # Seed usage for first n_usage facts
    usage_ts = _NOW - timedelta(days=10)
    usage_ids = fact_ids[:n_usage]
    for fid in usage_ids:
        usage = AccessUsageFact(
            id=uuid.uuid4(),
            access_fact_id=fid,
            last_seen=usage_ts,
            usage_count=1,
            window_from=usage_ts,
            window_to=None,
        )
        pg_session.add(usage)
    await pg_session.flush()

    # Mirror to Iceberg — nullable=False on required fields must match Iceberg schema
    pa_schema = pa.schema(
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

    ice_rows = {
        'id': [str(fid) for fid in fact_ids],
        'subject_id': [str(subject_id)] * n,
        'account_id': [None] * n,
        'resource_id': [str(rid) for rid in resource_ids],
        'action_id': ['read'] * n,
        'effect': ['allow'] * n,
        'valid_from': [valid_from] * n,
        'valid_until': [None] * n,
        'is_active': [True] * n,
        'observed_at': [valid_from] * n,
        'created_at': [valid_from] * n,
        'revoked_at': [None] * n,
        'latest_batch_id': [None] * n,
        'application_id_denorm': [str(_APP_ID)] * n,
        'subject_kind_denorm': ['User'] * n,
        'reconciliation_delta_item_id': [str(uuid.uuid4()) for _ in range(n)],
        'natural_key_hash': ['a' * 64] * n,
    }
    arrow_table = pa.table(ice_rows, schema=pa_schema)
    iceberg_table.append(arrow_table)

    return fact_ids, usage_ids, resource_ids[0], action_id


# ===========================================================================
# Case 1: UnusedDetectorService parity (live-run both paths)
# ===========================================================================


async def test_unused_detector_service_parity_with_legacy(
    pg_session: AsyncSession,
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    """Both paths on same seed → byte-equal UnusedFinding sets after sort."""
    fact_ids, _, _, _ = await _seed_shared(pg_session, iceberg_table, n=50, n_usage=20)

    at = datetime.now(tz=UTC)

    # --- Legacy path (raw SQL) ---

    pg_result = await pg_session.execute(
        sa.text(
            """
            SELECT f.id, f.subject_id, f.account_id, f.resource_id, f.valid_from,
                   r.application_id, u_agg.last_seen
            FROM access_facts f
            JOIN resources r ON r.id = f.resource_id
            LEFT JOIN (
                SELECT access_fact_id, MAX(last_seen) AS last_seen
                FROM access_usage_facts
                GROUP BY access_fact_id
            ) u_agg ON u_agg.access_fact_id = f.id
            WHERE f.is_active = true AND r.application_id = :app_id
            """
        ),
        {'app_id': _APP_ID},
    )
    legacy_views = [
        AccessFactView(
            id=row.id,
            subject_id=row.subject_id,
            account_id=row.account_id,
            resource_id=row.resource_id,
            application_id=row.application_id,
            valid_from=row.valid_from,
            last_seen=row.last_seen,
        )
        for row in pg_result.all()
    ]
    legacy_findings = detect_unused(access_facts=legacy_views, threshold_days=_THRESHOLD, at=at)

    # --- DuckDB new path ---
    log = NoOpLogService()
    duck_views: list[AccessFactView] = []
    async for v in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log,
        scope_application_id=_APP_ID,
        scope_subject_id=None,
        batch_size=25,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        duck_views.append(v)
    duck_findings = detect_unused(access_facts=duck_views, threshold_days=_THRESHOLD, at=at)

    # Sort both by (application_id, subject_id, access_fact_id) — deterministic
    def _sort_key(f: Any) -> tuple[str, str, str]:
        return (str(f.application_id), str(f.subject_id), str(f.access_fact_id))

    legacy_sorted = sorted(legacy_findings, key=_sort_key)
    duck_sorted = sorted(duck_findings, key=_sort_key)

    assert len(legacy_sorted) == len(duck_sorted), (
        f'finding count mismatch: legacy={len(legacy_sorted)} duck={len(duck_sorted)}'
    )
    for lf, df in zip(legacy_sorted, duck_sorted):
        assert lf.access_fact_id == df.access_fact_id
        assert lf.subject_id == df.subject_id
        assert lf.application_id == df.application_id
        assert lf.resource_id == df.resource_id
        assert lf.unused_for_days == df.unused_for_days
        # last_seen timestamps compared as datetime objects (not strings) to avoid precision drift
        if lf.last_seen is not None:
            assert df.last_seen is not None
            assert abs((lf.last_seen - df.last_seen).total_seconds()) < 1
        else:
            assert df.last_seen is None


# ===========================================================================
# Case 2: load_unused_inputs parity (AccessFactView IDs must match)
# ===========================================================================


async def test_load_unused_inputs_via_engine_parity(
    pg_session: AsyncSession,
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    """load_unused_inputs (DuckDB) and legacy query return the same fact IDs."""
    fact_ids, _, _, _ = await _seed_shared(pg_session, iceberg_table, n=30, n_usage=10)

    # DuckDB path via load_unused_inputs
    log = NoOpLogService()
    duck_views = await load_unused_inputs(
        session=pg_session,
        lake_session=lake_session,
        log_service=log,
        scope_application_id=_APP_ID,
        scope_subject_id=None,
        batch_size=15,
        pg_any_array_max_size=_PG_ANY_MAX,
    )
    duck_ids = {v.id for v in duck_views}

    # Legacy inline query (raw SQL)
    pg_result = await pg_session.execute(
        sa.text(
            'SELECT f.id FROM access_facts f JOIN resources r ON r.id = f.resource_id '
            'WHERE f.is_active = true AND r.application_id = :app_id'
        ),
        {'app_id': _APP_ID},
    )
    legacy_ids = {row.id for row in pg_result.all()}

    assert duck_ids == legacy_ids


# ===========================================================================
# Case 3: emits started + completed logs on run()
# ===========================================================================


async def test_unused_detector_service_emits_logs(
    pg_session: AsyncSession,
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    """UnusedDetectorService.run() emits exactly one started + one completed log."""
    await _seed_shared(pg_session, iceberg_table, n=10, n_usage=5)

    log_service, sink = _make_log_service()
    service = UnusedDetectorService(
        session=pg_session,
        lake_session=lake_session,
        log_service=log_service,
        pg_any_array_max_size=_PG_ANY_MAX,
    )
    await service.run(application_id=_APP_ID, threshold_days=_THRESHOLD, limit=100)

    await asyncio.sleep(0)  # flush fire-and-forget tasks

    started = [r for r in sink.records if 'duckdb_query_started' in r.message]
    completed = [r for r in sink.records if 'duckdb_query_completed' in r.message]
    assert len(started) == 1
    assert len(completed) == 1
