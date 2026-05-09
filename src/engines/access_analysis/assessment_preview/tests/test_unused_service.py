# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer integration tests for UnusedDetectorService — DB + Iceberg backed.

Each test seeds PG (access_facts, access_usage_facts) AND Iceberg (normalized.access_facts)
to exercise the new DuckDB read path. Usage telemetry remains in PG only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import tempfile
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
import sqlalchemy as sa
from src.engines.access_analysis.assessment_preview.service import UnusedDetectorService
from src.inventory.access_usage_facts.models import AccessUsageFact
from src.inventory.assessment.findings.models import Finding
from src.inventory.employees.repository import create_employee
from src.inventory.persons.repository import create_person
from src.inventory.policy.sod_rules.models import SodSeverity
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind
from src.platform.applications.models import Application
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_PG_ANY_MAX = 25000


# ---------------------------------------------------------------------------
# Lake fixture (per-test isolated warehouse)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_catalog() -> Any:  # noqa: ANN401
    reset_catalog_cache_for_tests()
    yield
    reset_catalog_cache_for_tests()


@pytest.fixture
def lake_session() -> Any:  # noqa: ANN401
    """Return a LakeSession with a fresh Iceberg warehouse for each test."""
    tmp_dir = tempfile.mkdtemp(prefix='aurelion_unused_svc_test_')
    settings = LakeSettings(
        catalog_url=f'sqlite:///{tmp_dir}/catalog.db',
        warehouse_uri=f'file://{tmp_dir}/warehouse',
        storage_provider='file',
    )
    log = NoOpLogService()

    reset_catalog_cache_for_tests()
    catalog = get_catalog(settings, log_service=log)  # type: ignore[arg-type]

    # Provision normalized.access_facts with string-based schema
    schema = Schema(
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
    spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
    )
    try:
        catalog.create_namespace(('normalized',))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass
    iceberg_table = catalog.create_table(('normalized', 'access_facts'), schema=schema, partition_spec=spec)
    reset_catalog_cache_for_tests()

    factory = LakeSessionFactory(settings=settings, log_service=log, pg_dsn=None)  # type: ignore[arg-type]
    session = factory.acquire()
    yield session, iceberg_table
    session.__exit__(None, None, None)
    factory.close_all()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _make_pa_schema() -> pa.Schema:
    return pa.schema(
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


def _seed_iceberg(
    iceberg_table: Any,
    facts: list[dict[str, Any]],
) -> None:
    """Write facts list to Iceberg table."""
    schema = _make_pa_schema()
    rows: dict[str, list[Any]] = {col: [] for col in schema.names}
    for f in facts:
        for col in schema.names:
            rows[col].append(f.get(col))
    iceberg_table.append(pa.table(rows, schema=schema))


async def _seed_application(session: Any) -> uuid.UUID:
    app = Application(
        name=f'app-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _seed_subject(session: Any) -> uuid.UUID:
    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _seed_resource(session: Any, app_id: uuid.UUID) -> uuid.UUID:
    resource = Resource(
        external_id=str(uuid.uuid4()),
        application_id=app_id,
        kind='database',
        resource_type='database',
        resource_key=str(uuid.uuid4()),
    )
    session.add(resource)
    await session.flush()
    return resource.id


async def _seed_access_fact(
    session: Any,
    *,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
    valid_from: datetime,
    is_active: bool = True,
) -> uuid.UUID:
    fact_id = uuid.uuid4()
    return fact_id


async def _seed_usage_fact(
    session: Any,
    *,
    access_fact_id: uuid.UUID,
    last_seen: datetime,
    window_from: datetime | None = None,
    window_to: datetime | None = None,
) -> None:
    if window_from is None:
        window_from = last_seen - timedelta(hours=1)
    usage = AccessUsageFact(
        access_fact_id=access_fact_id,
        last_seen=last_seen,
        usage_count=1,
        window_from=window_from,
        window_to=window_to,
    )
    session.add(usage)
    await session.flush()


def _make_iceberg_row(
    fact_id: uuid.UUID,
    subject_id: uuid.UUID,
    app_id: uuid.UUID,
    resource_id: uuid.UUID,
    valid_from: datetime,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        'id': str(fact_id),
        'subject_id': str(subject_id),
        'account_id': None,
        'resource_id': str(resource_id),
        'action_id': 'read',
        'effect': 'allow',
        'valid_from': valid_from,
        'valid_until': None,
        'is_active': is_active,
        'observed_at': _NOW,
        'created_at': _NOW,
        'revoked_at': None,
        'latest_batch_id': None,
        'application_id_denorm': str(app_id),
        'subject_kind_denorm': 'User',
        'reconciliation_delta_item_id': str(uuid.uuid4()),
        'natural_key_hash': 'a' * 64,
    }


# ---------------------------------------------------------------------------
# Test S1: basic scenario — 4 active facts + 1 inactive → 2 findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_returns_expected_findings(session_factory: Any, lake_session: Any) -> None:
    ls, iceberg_table = lake_session

    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)

        res_a = await _seed_resource(session, app_id)
        fact_a = await _seed_access_fact(
            session, subject_id=subj_id, resource_id=res_a, valid_from=_NOW - timedelta(days=150)
        )
        await _seed_usage_fact(session, access_fact_id=fact_a, last_seen=_NOW - timedelta(days=120))

        res_b = await _seed_resource(session, app_id)
        fact_b = await _seed_access_fact(
            session, subject_id=subj_id, resource_id=res_b, valid_from=_NOW - timedelta(days=50)
        )
        await _seed_usage_fact(session, access_fact_id=fact_b, last_seen=_NOW - timedelta(days=10))

        res_c = await _seed_resource(session, app_id)
        fact_c = await _seed_access_fact(
            session, subject_id=subj_id, resource_id=res_c, valid_from=_NOW - timedelta(days=200)
        )

        res_d = await _seed_resource(session, app_id)
        await _seed_access_fact(session, subject_id=subj_id, resource_id=res_d, valid_from=_NOW - timedelta(days=5))

        res_inactive = await _seed_resource(session, app_id)
        inactive_id = await _seed_access_fact(
            session,
            subject_id=subj_id,
            resource_id=res_inactive,
            valid_from=_NOW - timedelta(days=300),
            is_active=False,
        )
        await _seed_usage_fact(session, access_fact_id=inactive_id, last_seen=_NOW - timedelta(days=250))

        await session.commit()

    # Mirror active facts to Iceberg
    _seed_iceberg(
        iceberg_table,
        [
            _make_iceberg_row(fact_a, subj_id, app_id, res_a, _NOW - timedelta(days=150)),
            _make_iceberg_row(fact_b, subj_id, app_id, res_b, _NOW - timedelta(days=50)),
            _make_iceberg_row(fact_c, subj_id, app_id, res_c, _NOW - timedelta(days=200)),
            # inactive not mirrored (is_active=False filtered by WHERE in DuckDB)
            # fact_d also mirrored (active, but won't yield finding due to threshold)
        ],
    )

    async with session_factory() as session:
        svc = UnusedDetectorService(
            session=session,
            lake_session=ls,
            log_service=NoOpLogService(),
            pg_any_array_max_size=_PG_ANY_MAX,
        )
        findings = await svc.run(application_id=None, threshold_days=90, limit=1000)

    assert len(findings) == 2
    access_fact_ids = {f.access_fact_id for f in findings}
    assert fact_a in access_fact_ids
    assert fact_c in access_fact_ids
    for f in findings:
        assert f.severity == SodSeverity.low


# ---------------------------------------------------------------------------
# Test S2: MAX last_seen wins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_max_last_seen_used(session_factory: Any, lake_session: Any) -> None:
    ls, iceberg_table = lake_session

    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_id = await _seed_resource(session, app_id)
        fact_id = await _seed_access_fact(
            session, subject_id=subj_id, resource_id=res_id, valid_from=_NOW - timedelta(days=300)
        )
        await _seed_usage_fact(
            session,
            access_fact_id=fact_id,
            last_seen=_NOW - timedelta(days=200),
            window_from=_NOW - timedelta(days=210),
            window_to=_NOW - timedelta(days=200),
        )
        await _seed_usage_fact(
            session,
            access_fact_id=fact_id,
            last_seen=_NOW - timedelta(days=5),
            window_from=_NOW - timedelta(days=10),
            window_to=_NOW - timedelta(days=5),
        )
        await session.commit()

    _seed_iceberg(iceberg_table, [_make_iceberg_row(fact_id, subj_id, app_id, res_id, _NOW - timedelta(days=300))])

    async with session_factory() as session:
        svc = UnusedDetectorService(
            session=session,
            lake_session=ls,
            log_service=NoOpLogService(),
            pg_any_array_max_size=_PG_ANY_MAX,
        )
        findings = await svc.run(application_id=None, threshold_days=90, limit=1000)

    assert all(f.access_fact_id != fact_id for f in findings)


# ---------------------------------------------------------------------------
# Test S3: application_id filter — different app → empty list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_filters_by_application_id(session_factory: Any, lake_session: Any) -> None:
    ls, iceberg_table = lake_session

    async with session_factory() as session:
        app_a = await _seed_application(session)
        app_b = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_a = await _seed_resource(session, app_a)
        fact_id = await _seed_access_fact(
            session, subject_id=subj_id, resource_id=res_a, valid_from=_NOW - timedelta(days=200)
        )
        await session.commit()

    # Mirror to Iceberg with app_a
    _seed_iceberg(iceberg_table, [_make_iceberg_row(fact_id, subj_id, app_a, res_a, _NOW - timedelta(days=200))])

    async with session_factory() as session:
        svc = UnusedDetectorService(
            session=session,
            lake_session=ls,
            log_service=NoOpLogService(),
            pg_any_array_max_size=_PG_ANY_MAX,
        )
        findings = await svc.run(application_id=app_b, threshold_days=90, limit=1000)

    assert findings == []


# ---------------------------------------------------------------------------
# Test S4: aggressive threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_aggressive_threshold(session_factory: Any, lake_session: Any) -> None:
    ls, iceberg_table = lake_session

    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_id = await _seed_resource(session, app_id)
        fact_id = await _seed_access_fact(
            session, subject_id=subj_id, resource_id=res_id, valid_from=_NOW - timedelta(days=100)
        )
        await _seed_usage_fact(session, access_fact_id=fact_id, last_seen=_NOW - timedelta(days=2))
        await session.commit()

    _seed_iceberg(iceberg_table, [_make_iceberg_row(fact_id, subj_id, app_id, res_id, _NOW - timedelta(days=100))])

    async with session_factory() as session:
        svc = UnusedDetectorService(
            session=session,
            lake_session=ls,
            log_service=NoOpLogService(),
            pg_any_array_max_size=_PG_ANY_MAX,
        )
        findings = await svc.run(application_id=None, threshold_days=1, limit=1000)

    assert any(f.access_fact_id == fact_id for f in findings)


# ---------------------------------------------------------------------------
# Test S5: limit parameter respected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_respects_limit(session_factory: Any, lake_session: Any) -> None:
    ls, iceberg_table = lake_session

    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        fact_ids = []
        res_ids = []
        for _ in range(5):
            res_id = await _seed_resource(session, app_id)
            fact_id = await _seed_access_fact(
                session, subject_id=subj_id, resource_id=res_id, valid_from=_NOW - timedelta(days=200)
            )
            fact_ids.append(fact_id)
            res_ids.append(res_id)
        await session.commit()

    _seed_iceberg(
        iceberg_table,
        [
            _make_iceberg_row(fid, subj_id, app_id, rid, _NOW - timedelta(days=200))
            for fid, rid in zip(fact_ids, res_ids)
        ],
    )

    async with session_factory() as session:
        svc = UnusedDetectorService(
            session=session,
            lake_session=ls,
            log_service=NoOpLogService(),
            pg_any_array_max_size=_PG_ANY_MAX,
        )
        findings = await svc.run(application_id=None, threshold_days=90, limit=3)

    assert len(findings) <= 3


# ---------------------------------------------------------------------------
# Test S6: service does not write Finding rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_does_not_persist(session_factory: Any, lake_session: Any) -> None:
    ls, iceberg_table = lake_session

    async with session_factory() as session:
        app_id = await _seed_application(session)
        subj_id = await _seed_subject(session)
        res_id = await _seed_resource(session, app_id)
        fact_id = await _seed_access_fact(
            session, subject_id=subj_id, resource_id=res_id, valid_from=_NOW - timedelta(days=200)
        )
        await session.commit()

    _seed_iceberg(iceberg_table, [_make_iceberg_row(fact_id, subj_id, app_id, res_id, _NOW - timedelta(days=200))])

    async with session_factory() as session:
        svc = UnusedDetectorService(
            session=session,
            lake_session=ls,
            log_service=NoOpLogService(),
            pg_any_array_max_size=_PG_ANY_MAX,
        )
        findings = await svc.run(application_id=None, threshold_days=90, limit=1000)
        assert len(findings) >= 1

        count = await session.scalar(sa.select(sa.func.count()).select_from(Finding))
        assert count == 0
