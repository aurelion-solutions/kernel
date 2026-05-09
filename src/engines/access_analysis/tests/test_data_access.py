# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/engines/access_analysis/data_access.py.

All lake-side tests use an in-process DuckDB + tmp-dir Iceberg warehouse.
PG-side (access_usage_facts) is stubbed via AsyncMock — the usage lookup is
an implementation detail of _fetch_usage_map; real FK-constrained inserts
are covered in test_service_lake.py which seeds full PG + Iceberg data.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
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
from src.engines.access_analysis.data_access import iter_unused_access_fact_views
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.logs.service import LogService, NoOpLogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)
_APP1 = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000001')
_APP2 = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000002')
_SUBJ1 = uuid.UUID('bbbbbbbb-0000-0000-0000-000000000001')
_SUBJ2 = uuid.UUID('bbbbbbbb-0000-0000-0000-000000000002')
_RES1 = uuid.UUID('cccccccc-0000-0000-0000-000000000001')

_PG_ANY_MAX = 25000
_DEFAULT_BATCH = 10


# ---------------------------------------------------------------------------
# Lake fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_catalog_cache() -> Any:  # noqa: ANN401
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


def _make_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


def _noop_log() -> NoOpLogService:
    return NoOpLogService()


def _make_schema() -> Schema:
    """Return a string-based schema for normalized.access_facts test table.

    UUID fields stored as StringType to work around PyArrow 24 group_by
    limitation on extension<arrow.uuid> columns in UUID-partitioned tables.
    """
    return Schema(
        NestedField(1, 'id', StringType(), required=True),
        NestedField(2, 'subject_id', StringType(), required=False),
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


def _make_partition_spec() -> PartitionSpec:
    return PartitionSpec(
        PartitionField(
            source_id=15,
            field_id=1000,
            transform=IdentityTransform(),
            name='subject_kind_denorm',
        )
    )


def _make_fact_row(
    fact_id: uuid.UUID,
    subject_id: uuid.UUID | None,
    application_id: uuid.UUID | None,
    resource_id: uuid.UUID = _RES1,
    is_active: bool = True,
    valid_from: datetime | None = None,
) -> dict[str, Any]:
    return {
        'id': str(fact_id),
        'subject_id': str(subject_id) if subject_id is not None else None,
        'account_id': None,
        'resource_id': str(resource_id),
        'action_id': 'read',
        'effect': 'allow',
        'valid_from': valid_from or _NOW - timedelta(days=120),
        'valid_until': None,
        'is_active': is_active,
        'observed_at': _NOW - timedelta(days=120),
        'created_at': _NOW - timedelta(days=120),
        'revoked_at': None,
        'latest_batch_id': None,
        'application_id_denorm': str(application_id) if application_id else None,
        'subject_kind_denorm': 'User',
        'reconciliation_delta_item_id': str(uuid.uuid4()),
        'natural_key_hash': 'a' * 64,
    }


def _write_rows(table: Any, rows: list[dict[str, Any]]) -> None:
    """Write rows to an Iceberg table via PyArrow.

    nullable=False on required fields must match the Iceberg schema (required=True)
    to pass PyIceberg's schema validation on append.
    """
    schema = pa.schema(
        [
            pa.field('id', pa.string(), nullable=False),
            pa.field('subject_id', pa.string(), nullable=True),
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

    arrow_rows = {col: [row.get(col) for row in rows] for col in schema.names}
    arrow_table = pa.table(arrow_rows, schema=schema)
    table.append(arrow_table)


@pytest.fixture
def iceberg_table(lake_settings: LakeSettings) -> Any:  # noqa: ANN401
    log, _ = _make_log_service()
    cat = get_catalog(lake_settings, log_service=log)

    try:
        cat.create_namespace(('normalized',))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    identifier = ('normalized', 'access_facts')
    try:
        cat.drop_table(identifier)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    tbl = cat.create_table(identifier, schema=_make_schema(), partition_spec=_make_partition_spec())
    return tbl


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
# PG session stub — returns empty usage map by default
# ---------------------------------------------------------------------------


def _make_empty_pg_session() -> Any:  # noqa: ANN401
    """Return an AsyncMock session that returns empty usage results.

    data_access calls: pg_session.execute(sql, params) → result.all()
    We stub the chain so it returns an empty list.
    """
    mock_result = MagicMock()
    mock_result.all.return_value = []
    session = AsyncMock()
    session.execute = AsyncMock(return_value=mock_result)
    return session


def _make_pg_session_with_usage(
    usage_map: dict[uuid.UUID, datetime],
) -> Any:  # noqa: ANN401
    """Return an AsyncMock session that returns the provided usage_map rows."""

    class _FakeRow:
        def __init__(self, fid: uuid.UUID, ts: datetime) -> None:
            self.access_fact_id = fid
            self.last_seen = ts

    async def _execute_side_effect(sql: Any, params: Any = None) -> Any:  # noqa: ANN401
        rows = [_FakeRow(fid, ts) for fid, ts in usage_map.items()]
        mock_result = MagicMock()
        mock_result.all.return_value = rows
        return mock_result

    session = AsyncMock()
    session.execute.side_effect = _execute_side_effect
    return session


# ===========================================================================
# Test 1: emits views in deterministic order, last_seen populated correctly
# ===========================================================================


async def test_iter_unused_emits_views_in_order(
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    """50 rows seed: mix of active/inactive + 2 apps. Expect active only, sorted."""
    fact_ids_app1 = [uuid.uuid4() for _ in range(15)]
    fact_ids_app2 = [uuid.uuid4() for _ in range(15)]
    inactive_ids = [uuid.uuid4() for _ in range(20)]

    rows = (
        [_make_fact_row(fid, _SUBJ1, _APP1) for fid in fact_ids_app1]
        + [_make_fact_row(fid, _SUBJ2, _APP2) for fid in fact_ids_app2]
        + [_make_fact_row(fid, _SUBJ1, _APP1, is_active=False) for fid in inactive_ids]
    )
    _write_rows(iceberg_table, rows)

    # Stub usage for first 5 from each app
    usage_fact_ids = fact_ids_app1[:5] + fact_ids_app2[:5]
    usage_ts = _NOW - timedelta(days=10)
    usage_data = {fid: usage_ts for fid in usage_fact_ids}
    pg_session = _make_pg_session_with_usage(usage_data)

    log = _noop_log()
    views = []
    async for v in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log,
        scope_application_id=None,
        scope_subject_id=None,
        batch_size=_DEFAULT_BATCH,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        views.append(v)

    # Only active rows
    assert len(views) == 30
    # Sorted by (application_id_denorm, subject_id, id)
    expected_key = [(str(v.application_id), str(v.subject_id), str(v.id)) for v in views]
    assert expected_key == sorted(expected_key)
    # last_seen populated for usage_fact_ids
    views_with_usage = [v for v in views if v.id in set(usage_fact_ids)]
    assert all(v.last_seen == usage_ts for v in views_with_usage)
    views_without_usage = [v for v in views if v.id not in set(usage_fact_ids)]
    assert all(v.last_seen is None for v in views_without_usage)


# ===========================================================================
# Test 2: scope_application_id filter
# ===========================================================================


async def test_iter_unused_filters_by_application(
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    ids_a1 = [uuid.uuid4() for _ in range(10)]
    ids_a2 = [uuid.uuid4() for _ in range(10)]
    rows = [_make_fact_row(fid, _SUBJ1, _APP1) for fid in ids_a1] + [
        _make_fact_row(fid, _SUBJ2, _APP2) for fid in ids_a2
    ]
    _write_rows(iceberg_table, rows)

    pg_session = _make_empty_pg_session()
    log = _noop_log()
    views = []
    async for v in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log,
        scope_application_id=_APP1,
        scope_subject_id=None,
        batch_size=_DEFAULT_BATCH,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        views.append(v)

    assert len(views) == 10
    assert all(v.application_id == _APP1 for v in views)


# ===========================================================================
# Test 3: scope_subject_id filter
# ===========================================================================


async def test_iter_unused_filters_by_subject(
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    ids_s1 = [uuid.uuid4() for _ in range(8)]
    ids_s2 = [uuid.uuid4() for _ in range(8)]
    rows = [_make_fact_row(fid, _SUBJ1, _APP1) for fid in ids_s1] + [
        _make_fact_row(fid, _SUBJ2, _APP1) for fid in ids_s2
    ]
    _write_rows(iceberg_table, rows)

    pg_session = _make_empty_pg_session()
    log = _noop_log()
    views = []
    async for v in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log,
        scope_application_id=None,
        scope_subject_id=_SUBJ1,
        batch_size=_DEFAULT_BATCH,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        views.append(v)

    assert len(views) == 8
    assert all(v.subject_id == _SUBJ1 for v in views)


# ===========================================================================
# Test 4: uses fetchmany in batches (monkeypatch counter)
# ===========================================================================


async def test_iter_unused_uses_fetchmany_in_batches(
    iceberg_table: Any,
    lake_session: LakeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """50 rows, batch_size=10 → exactly 5 fetchmany calls; fetchall never called."""
    ids = [uuid.uuid4() for _ in range(50)]
    rows = [_make_fact_row(fid, _SUBJ1, _APP1) for fid in ids]
    _write_rows(iceberg_table, rows)

    fetchmany_calls: list[int] = []
    fetchall_calls: list[int] = []

    original_execute = lake_session.execute

    class _TrackingCursor:
        """Wraps a DuckDB cursor to track fetchmany/fetchall calls."""

        def __init__(self, real_cursor: Any) -> None:
            self._cursor = real_cursor

        def fetchmany(self, n: int) -> Any:
            fetchmany_calls.append(n)
            return self._cursor.fetchmany(n)

        def fetchall(self) -> Any:
            fetchall_calls.append(1)
            return self._cursor.fetchall()

    def patched_execute(sql: str, params: list[Any] | None = None) -> Any:
        cursor = original_execute(sql, params)
        return _TrackingCursor(cursor)

    monkeypatch.setattr(lake_session, 'execute', patched_execute)

    pg_session = _make_empty_pg_session()
    log = _noop_log()
    views = []
    async for v in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log,
        scope_application_id=None,
        scope_subject_id=None,
        batch_size=10,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        views.append(v)

    assert len(views) == 50
    # 5 batches of 10 + 1 empty batch terminates the loop
    assert len(fetchmany_calls) == 6
    assert fetchmany_calls[:5] == [10, 10, 10, 10, 10]
    # fetchall must never be called
    assert len(fetchall_calls) == 0


# ===========================================================================
# Test 5: null application_id_denorm rows skipped + debug log emitted
# ===========================================================================


async def test_iter_unused_skips_null_application_id_with_debug_log(
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    """One row with null application_id_denorm — must be skipped; DEBUG log emitted."""
    null_id = uuid.uuid4()
    normal_id = uuid.uuid4()
    rows = [
        _make_fact_row(null_id, _SUBJ1, None),  # null application_id_denorm
        _make_fact_row(normal_id, _SUBJ1, _APP1),
    ]
    _write_rows(iceberg_table, rows)

    pg_session = _make_empty_pg_session()
    log_service, sink = _make_log_service()
    views = []
    async for v in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log_service,
        scope_application_id=None,
        scope_subject_id=None,
        batch_size=_DEFAULT_BATCH,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        views.append(v)

    assert len(views) == 1
    assert views[0].id == normal_id

    await asyncio.sleep(0)  # let fire-and-forget tasks flush

    debug_records = [r for r in sink.records if 'unused_row_skipped_null_application_id' in r.message]
    assert len(debug_records) == 1


# ===========================================================================
# Test 5b: null subject_id rows skipped + debug log emitted
# ===========================================================================


async def test_iter_unused_skips_null_subject_id_with_debug_log(
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    """One row with null subject_id (orphan-fact) — must be skipped; DEBUG log emitted."""
    null_id = uuid.uuid4()
    normal_id = uuid.uuid4()
    rows = [
        _make_fact_row(null_id, None, _APP1),  # null subject_id
        _make_fact_row(normal_id, _SUBJ1, _APP1),
    ]
    _write_rows(iceberg_table, rows)

    pg_session = _make_empty_pg_session()
    log_service, sink = _make_log_service()
    views = []
    async for v in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log_service,
        scope_application_id=None,
        scope_subject_id=None,
        batch_size=_DEFAULT_BATCH,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        views.append(v)

    assert len(views) == 1
    assert views[0].id == normal_id

    await asyncio.sleep(0)

    debug_records = [r for r in sink.records if 'unused_row_skipped_null_subject_id' in r.message]
    assert len(debug_records) == 1


# ===========================================================================
# Test 6: started + completed log emitted exactly once
# ===========================================================================


async def test_iter_unused_emits_started_and_completed_log_once(
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    ids = [uuid.uuid4() for _ in range(5)]
    rows = [_make_fact_row(fid, _SUBJ1, _APP1) for fid in ids]
    _write_rows(iceberg_table, rows)

    pg_session = _make_empty_pg_session()
    log_service, sink = _make_log_service()
    async for _ in iter_unused_access_fact_views(
        lake_session=lake_session,
        pg_session=pg_session,
        log_service=log_service,
        scope_application_id=None,
        scope_subject_id=None,
        batch_size=_DEFAULT_BATCH,
        pg_any_array_max_size=_PG_ANY_MAX,
    ):
        pass

    await asyncio.sleep(0)

    started = [r for r in sink.records if 'duckdb_query_started' in r.message]
    completed = [r for r in sink.records if 'duckdb_query_completed' in r.message]
    assert len(started) == 1
    assert len(completed) == 1


# ===========================================================================
# Test 7: rejects batch_size > pg_any_array_max_size
# ===========================================================================


async def test_iter_unused_rejects_oversize_batch(
    iceberg_table: Any,
    lake_session: LakeSession,
) -> None:
    pg_session = _make_empty_pg_session()
    log = _noop_log()
    with pytest.raises(ValueError, match='batch_size.*LAKE_PG_ANY_ARRAY_MAX_SIZE'):
        async for _ in iter_unused_access_fact_views(
            lake_session=lake_session,
            pg_session=pg_session,
            log_service=log,
            scope_application_id=None,
            scope_subject_id=None,
            batch_size=_PG_ANY_MAX + 1,
            pg_any_array_max_size=_PG_ANY_MAX,
        ):
            pass
