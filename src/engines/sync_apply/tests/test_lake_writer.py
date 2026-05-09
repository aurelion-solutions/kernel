# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/engines/sync_apply/lake_writer.py.

All tests use an in-process SQLite-backed PyIceberg catalog + tmp-dir warehouse.
No network, no real PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import subprocess
from typing import Any
import uuid

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
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaOperation,
)
from src.engines.sync_apply.lake_writer import (
    LakeWriterError,
    PreflightRecoveryResult,
    RunWriteResult,
    preflight_recover_already_written,
    write_run_batch,
)
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_NOW_ISO = _NOW.isoformat()

_APP_ID = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000001')
_SUBJECT_ID = uuid.UUID('bbbbbbbb-0000-0000-0000-000000000002')
_RESOURCE_ID = uuid.UUID('cccccccc-0000-0000-0000-000000000003')
_ACTION_ID = 42


def _make_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


def _after_json(
    *,
    effect: str = 'allow',
    valid_from: str | None = None,
    observed_at: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        'effect': effect,
        'valid_from': valid_from or _NOW_ISO,
        'observed_at': observed_at or _NOW_ISO,
        'created_at': created_at or _NOW_ISO,
        'valid_until': None,
        'revoked_at': None,
        'latest_batch_id': None,
    }


def _make_item(
    *,
    operation: ReconciliationDeltaOperation = ReconciliationDeltaOperation.create,
    item_id: uuid.UUID | None = None,
    natural_key_hash: str = 'a' * 64,
    subject_id: uuid.UUID | None = None,
    after_json: dict[str, Any] | None = None,
    before_json: dict[str, Any] | None = None,
) -> ReconciliationDeltaItem:
    """Build a transient (not persisted) ReconciliationDeltaItem for unit tests.

    Uses SQLAlchemy's keyword constructor which does not require a session or
    primary-key DB roundtrip — the object exists purely in memory.
    """
    return ReconciliationDeltaItem(
        id=item_id or uuid.uuid4(),
        reconciliation_run_id=uuid.uuid4(),
        operation=operation,
        natural_key_hash=natural_key_hash,
        subject_id=subject_id or _SUBJECT_ID,
        account_id=None,
        resource_id=_RESOURCE_ID,
        action_id=_ACTION_ID,
        effect='allow',
        existing_fact_id=None,
        source_artifact_id=None,
        before_json=before_json,
        after_json=after_json if after_json is not None else _after_json(),
        status=None,
        reason=None,
        created_at=_NOW,
        applied_at=None,
    )


def _simple_denorm(item: ReconciliationDeltaItem) -> tuple[str, str]:
    """Minimal denorm_resolver: always returns a fixed application_id + subject_kind."""
    return str(_APP_ID), 'User'


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
def catalog(lake_settings: LakeSettings) -> Any:  # noqa: ANN401
    """Return a catalog with a string-partition ``normalized.access_facts`` test table.

    NOTE: PyArrow 24 does not support ``group_by`` on ``extension<arrow.uuid>``
    partition columns, which PyIceberg requires when writing to UUID-partitioned
    tables.  The production partition spec uses ``application_id_denorm`` (UUIDType),
    which triggers this limitation.  This fixture creates the same table with
    ALL UUID fields declared as ``StringType()`` and partitioned only on
    ``subject_kind_denorm`` (a string field), matching the workaround used by
    ``test_service_lake.py`` (access_artifacts) and the maintenance_table fixture.
    The business logic under test (bucketing, snapshot count, delta_item_id
    tracking) is identical regardless of UUID vs string storage.
    """
    log, _ = _make_log_service()
    cat = get_catalog(lake_settings, log_service=log)

    # Bootstrap raw namespace (required by get_catalog bootstrap).
    for ns in (('raw',), ('normalized',)):
        try:
            cat.create_namespace(ns)
        except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
            pass

    # All UUID fields stored as StringType to avoid PyArrow 24 group_by limitation.
    test_schema = Schema(
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
        NestedField(14, 'application_id_denorm', StringType(), required=True),
        NestedField(15, 'subject_kind_denorm', StringType(), required=True),
        NestedField(16, 'reconciliation_delta_item_id', StringType(), required=True),
        NestedField(17, 'natural_key_hash', StringType(), required=True),
    )
    test_spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
    )
    identifier = ('normalized', 'access_facts')
    try:
        cat.drop_table(identifier)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass
    cat.create_table(identifier, schema=test_schema, partition_spec=test_spec)
    return cat


@pytest.fixture
def lake_session_factory(lake_settings: LakeSettings) -> LakeSessionFactory:
    log, _ = _make_log_service()
    return LakeSessionFactory(settings=lake_settings, log_service=log, pg_dsn=None)


@pytest.fixture
def lake_session(lake_session_factory: LakeSessionFactory) -> LakeSession:
    return lake_session_factory.acquire()


# ---------------------------------------------------------------------------
# Test 1: grouping by operation creates at most 4 snapshots
# ---------------------------------------------------------------------------


def test_write_run_batch_groups_by_operation_creates_at_most_four_snapshots(
    catalog: Any,
    lake_session: LakeSession,
) -> None:
    """2 create + 2 revoke + 1 reactivate → exactly 3 distinct snapshot IDs; 5 visible rows."""
    log, _ = _make_log_service()

    create_item_1 = _make_item(
        operation=ReconciliationDeltaOperation.create,
        natural_key_hash='c' * 64,
        item_id=uuid.UUID('11111111-0000-0000-0000-000000000001'),
    )
    create_item_2 = _make_item(
        operation=ReconciliationDeltaOperation.create,
        natural_key_hash='d' * 64,
        item_id=uuid.UUID('11111111-0000-0000-0000-000000000002'),
    )
    revoke_item_1 = _make_item(
        operation=ReconciliationDeltaOperation.revoke,
        natural_key_hash='e' * 64,
        item_id=uuid.UUID('11111111-0000-0000-0000-000000000003'),
        after_json=None,
        before_json=_after_json(),
    )
    revoke_item_2 = _make_item(
        operation=ReconciliationDeltaOperation.revoke,
        natural_key_hash='f' * 64,
        item_id=uuid.UUID('11111111-0000-0000-0000-000000000004'),
        after_json=None,
        before_json=_after_json(),
    )
    reactivate_item = _make_item(
        operation=ReconciliationDeltaOperation.reactivate,
        natural_key_hash='a' * 64,
        item_id=uuid.UUID('11111111-0000-0000-0000-000000000005'),
    )

    items = [create_item_1, create_item_2, revoke_item_1, revoke_item_2, reactivate_item]
    result = write_run_batch(items, catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

    assert isinstance(result, RunWriteResult)
    assert len(result.snapshot_ids) == 3
    assert 'create' in result.snapshot_ids
    assert 'revoke' in result.snapshot_ids
    assert 'reactivate' in result.snapshot_ids
    assert result.create_count == 2
    assert result.revoke_count == 2
    assert result.reactivate_count == 1

    # Verify all snapshot IDs are distinct integers.
    snap_vals = list(result.snapshot_ids.values())
    assert len(snap_vals) == len(set(snap_vals))

    # Verify 5 visible rows via DuckDB iceberg_scan.
    table_path = lake_session.iceberg_table_path('normalized', 'access_facts')
    lake_session.execute(f"SELECT COUNT(*) FROM iceberg_scan('{table_path}')")
    row = lake_session.fetchone()
    assert row is not None
    assert row[0] == 5


# ---------------------------------------------------------------------------
# Test 2: reconciliation_delta_item_id present in every Iceberg row
# ---------------------------------------------------------------------------


def test_write_run_batch_carries_delta_item_id(
    catalog: Any,
    lake_session: LakeSession,
) -> None:
    """The resulting Iceberg row must carry reconciliation_delta_item_id equal to item.id."""
    log, _ = _make_log_service()
    target_id = uuid.UUID('22222222-0000-0000-0000-000000000001')
    item = _make_item(
        operation=ReconciliationDeltaOperation.create,
        item_id=target_id,
        natural_key_hash='b' * 64,
    )

    write_run_batch([item], catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

    table_path = lake_session.iceberg_table_path('normalized', 'access_facts')
    lake_session.execute(f"SELECT reconciliation_delta_item_id FROM iceberg_scan('{table_path}')")
    rows = lake_session.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == str(target_id)


# ---------------------------------------------------------------------------
# Test 3: noop items are silently skipped
# ---------------------------------------------------------------------------


def test_write_run_batch_skips_noop_items(
    catalog: Any,
    lake_session: LakeSession,
) -> None:
    """1 create + 1 noop → 1 snapshot, 1 row; noop produces no Iceberg row."""
    log, _ = _make_log_service()
    create_item = _make_item(
        operation=ReconciliationDeltaOperation.create,
        natural_key_hash='1' * 64,
        item_id=uuid.UUID('33333333-0000-0000-0000-000000000001'),
    )
    noop_item = _make_item(
        operation=ReconciliationDeltaOperation.noop,
        natural_key_hash='2' * 64,
        item_id=uuid.UUID('33333333-0000-0000-0000-000000000002'),
    )

    result = write_run_batch([create_item, noop_item], catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

    assert result.create_count == 1
    assert 'noop' not in result.snapshot_ids
    assert len(result.snapshot_ids) == 1

    table_path = lake_session.iceberg_table_path('normalized', 'access_facts')
    lake_session.execute(f"SELECT COUNT(*) FROM iceberg_scan('{table_path}')")
    row = lake_session.fetchone()
    assert row is not None
    assert row[0] == 1


# ---------------------------------------------------------------------------
# Test 4: empty items list raises LakeWriterError
# ---------------------------------------------------------------------------


def test_write_run_batch_raises_on_empty_input(catalog: Any) -> None:
    """Calling write_run_batch([]) is a programming error and raises LakeWriterError."""
    log, _ = _make_log_service()
    with pytest.raises(LakeWriterError, match='empty items list'):
        write_run_batch([], catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)


# ---------------------------------------------------------------------------
# Test 5: item with empty natural_key_hash raises LakeWriterError
# ---------------------------------------------------------------------------


def test_write_run_batch_raises_on_missing_natural_key_hash(catalog: Any) -> None:
    """Item with empty natural_key_hash triggers LakeWriterError; no Iceberg commit happens."""
    log, _ = _make_log_service()
    item = _make_item(
        operation=ReconciliationDeltaOperation.create,
        natural_key_hash='',
    )

    with pytest.raises(LakeWriterError, match='natural_key_hash is empty'):
        write_run_batch([item], catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

    # Confirm no snapshot was committed.
    iceberg_table = catalog.load_table(('normalized', 'access_facts'))
    assert iceberg_table.current_snapshot() is None


# ---------------------------------------------------------------------------
# Test 6: preflight_recover_already_written happy path
# ---------------------------------------------------------------------------


def test_preflight_recovers_already_written_items(
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
) -> None:
    """Pre-seed 2 rows; preflight finds exactly those 2 ids; DuckDB session queried once."""
    log, _ = _make_log_service()

    delta_id_1 = uuid.UUID('44444444-0000-0000-0000-000000000001')
    delta_id_2 = uuid.UUID('44444444-0000-0000-0000-000000000002')
    delta_id_3 = uuid.UUID('44444444-0000-0000-0000-000000000003')  # not written

    # Write 2 items to Iceberg.
    item_1 = _make_item(
        operation=ReconciliationDeltaOperation.create,
        item_id=delta_id_1,
        natural_key_hash='p' * 64,
    )
    item_2 = _make_item(
        operation=ReconciliationDeltaOperation.create,
        item_id=delta_id_2,
        natural_key_hash='q' * 64,
    )
    write_run_batch([item_1, item_2], catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

    # 3rd item never written.
    item_3 = _make_item(
        operation=ReconciliationDeltaOperation.create,
        item_id=delta_id_3,
        natural_key_hash='r' * 64,
    )

    # Track DuckDB execute call count via a real session with a spy.
    real_session = lake_session_factory.acquire()
    execute_calls: list[tuple[str, Any]] = []
    original_execute = real_session.execute

    def spy_execute(sql: str, params: Any = None) -> Any:
        execute_calls.append((sql, params))
        return original_execute(sql, params)

    real_session.execute = spy_execute  # type: ignore[method-assign]

    result = preflight_recover_already_written([item_1, item_2, item_3], lake_session=real_session)

    assert isinstance(result, PreflightRecoveryResult)
    assert delta_id_1 in result.recovered_ids
    assert delta_id_2 in result.recovered_ids
    assert delta_id_3 not in result.recovered_ids
    assert len(result.recovered_ids) == 2

    # Exactly one DuckDB query issued.
    assert len(execute_calls) == 1
    sql_issued, _ = execute_calls[0]
    assert 'iceberg_scan' in sql_issued
    assert 'reconciliation_delta_item_id' in sql_issued


# ---------------------------------------------------------------------------
# Test 7: preflight returns empty set when no overlap
# ---------------------------------------------------------------------------


def test_preflight_returns_empty_when_no_overlap(
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
) -> None:
    """Pre-seed Iceberg with unrelated rows; preflight returns empty set."""
    log, _ = _make_log_service()

    # Write an item with a completely different delta_id.
    unrelated_id = uuid.UUID('55555555-0000-0000-0000-000000000001')
    unrelated_item = _make_item(
        operation=ReconciliationDeltaOperation.create,
        item_id=unrelated_id,
        natural_key_hash='x' * 64,
    )
    write_run_batch([unrelated_item], catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

    # Check for a completely different item.
    pending_id = uuid.UUID('55555555-0000-0000-0000-000000000002')
    pending_item = _make_item(
        operation=ReconciliationDeltaOperation.create,
        item_id=pending_id,
        natural_key_hash='y' * 64,
    )

    session = lake_session_factory.acquire()
    result = preflight_recover_already_written([pending_item], lake_session=session)

    assert isinstance(result, PreflightRecoveryResult)
    assert len(result.recovered_ids) == 0


# ---------------------------------------------------------------------------
# Test 8: 50 revoke items emit exactly 1 snapshot
# ---------------------------------------------------------------------------


def test_revoke_only_emits_one_snapshot_for_many_revokes(catalog: Any) -> None:
    """50 revoke items in one call → exactly 1 snapshot."""
    log, _ = _make_log_service()

    items = [
        _make_item(
            operation=ReconciliationDeltaOperation.revoke,
            natural_key_hash=str(i).zfill(64)[:64],
            item_id=uuid.uuid4(),
            after_json=None,
            before_json=_after_json(),
        )
        for i in range(50)
    ]

    result = write_run_batch(items, catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

    assert result.revoke_count == 50
    assert list(result.snapshot_ids.keys()) == ['revoke']
    assert len(result.snapshot_ids) == 1


# ---------------------------------------------------------------------------
# Test 9: lake_writer is imported only from sync_apply
# ---------------------------------------------------------------------------


def test_lake_writer_imported_only_from_sync_apply() -> None:
    """Grep src/ for imports of lake_writer; all hitting files must be inside sync_apply/."""
    kernel_src = Path(__file__).parent.parent.parent.parent
    # Use subprocess grep for reliability across platforms.
    result = subprocess.run(
        [
            'grep',
            '-r',
            '--include=*.py',
            '-l',
            'lake_writer',
            str(kernel_src),
        ],
        capture_output=True,
        text=True,
    )
    hitting_files = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and '__pycache__' not in line and '.ruff_cache' not in line
    ]

    sync_apply_path = str(kernel_src / 'engines' / 'sync_apply')
    for filepath in hitting_files:
        assert filepath.startswith(sync_apply_path), (
            f'lake_writer imported outside sync_apply/: {filepath}\nAll importing files: {hitting_files}'
        )
