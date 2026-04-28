# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/platform/lake/maintenance.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from typing import TYPE_CHECKING
import urllib.parse
import uuid

import pyarrow as pa
import pytest
from src.platform.lake.exceptions import LakeMaintenanceError
from src.platform.lake.maintenance import (
    CleanOrphanFilesResult,
    CompactTableResult,
    ExpireSnapshotsResult,
    clean_orphan_files,
    compact_table,
    expire_old_snapshots,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

if TYPE_CHECKING:
    from pyiceberg.table import Table

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)

# PyArrow schema for the lightweight test table defined in the maintenance_table
# fixture (test.artifacts): string-only partition to avoid PyArrow 24 UUID
# extension group_by limitation.
_TEST_PA_SCHEMA = pa.schema(
    [
        pa.field('id', pa.large_utf8(), nullable=False),
        pa.field('application_id', pa.large_utf8(), nullable=False),
        pa.field('artifact_type', pa.large_utf8(), nullable=False),
        pa.field('external_id', pa.large_utf8(), nullable=True),
        pa.field('is_active', pa.bool_(), nullable=False),
        pa.field('observed_at', pa.timestamp('us', tz='UTC'), nullable=False),
        pa.field('ingested_at', pa.timestamp('us', tz='UTC'), nullable=False),
    ]
)


def _make_arrow_row(
    *,
    row_id: str | None = None,
    application_id: str = 'aaaaaaaa-0000-0000-0000-000000000001',
    artifact_type: str = 'test_type',
    external_id: str | None = None,
) -> pa.Table:
    """Build a single-row PyArrow table compatible with the ``test.artifacts`` schema.

    The maintenance_table fixture uses a lightweight test table with a string-only
    partition (``artifact_type``) to avoid a PyArrow 24 limitation: ``group_by``
    does not support ``extension<arrow.uuid>`` keys used for UUID partition columns
    in the production ``raw.access_artifacts`` table.
    """
    if row_id is None:
        row_id = str(uuid.uuid4())
    if external_id is None:
        external_id = str(uuid.uuid4())

    ts = _NOW
    return pa.table(
        {
            'id': pa.array([row_id], type=pa.large_utf8()),
            'application_id': pa.array([application_id], type=pa.large_utf8()),
            'artifact_type': pa.array([artifact_type], type=pa.large_utf8()),
            'external_id': pa.array([external_id], type=pa.large_utf8()),
            'is_active': pa.array([True], type=pa.bool_()),
            'observed_at': pa.array([ts], type=pa.timestamp('us', tz='UTC')),
            'ingested_at': pa.array([ts], type=pa.timestamp('us', tz='UTC')),
        },
        schema=_TEST_PA_SCHEMA,
    )


def _append_rows(table: Table, count: int = 1) -> None:
    """Append ``count`` separate single-row batches to produce ``count`` snapshots/files."""
    for _ in range(count):
        table.append(_make_arrow_row())


# ---------------------------------------------------------------------------
# T1 — compact_table reduces file count
# ---------------------------------------------------------------------------


def test_compact_table_reduces_file_count(
    maintenance_table: Table,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """T1: 5 small separate appends → compact → files_after < 5."""
    log, sink = capturing_log_service
    # Seed 5 separate single-row batches → 5 data files.
    _append_rows(maintenance_table, count=5)
    maintenance_table.refresh()

    sink.clear()
    result = compact_table(maintenance_table, log_service=log)

    assert isinstance(result, CompactTableResult)
    assert result.files_before == 5
    assert result.files_after < result.files_before
    assert result.snapshot_id is not None

    # Verify exactly one compaction_completed INFO log.
    completed_logs = [r for r in sink.records if r.message == 'platform.lake.compaction_completed']
    assert len(completed_logs) == 1
    assert completed_logs[0].level == LogLevel.INFO

    # Row count is conserved — scan the table.
    arrow = maintenance_table.scan().to_arrow()
    assert arrow.num_rows == 5


# ---------------------------------------------------------------------------
# T2 — compact_table is idempotent
# ---------------------------------------------------------------------------


def test_compact_table_is_idempotent(
    maintenance_table: Table,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """T2: second call after compaction is a no-op."""
    log, sink = capturing_log_service
    _append_rows(maintenance_table, count=5)
    maintenance_table.refresh()

    sink.clear()
    compact_table(maintenance_table, log_service=log)

    maintenance_table.refresh()
    sink.clear()
    result2 = compact_table(maintenance_table, log_service=log)

    # No new rewrite — files_after should equal files_before.
    assert result2.files_after == result2.files_before

    # Second call emits exactly 1 INFO log.
    completed_logs = [r for r in sink.records if r.message == 'platform.lake.compaction_completed']
    assert len(completed_logs) == 1


# ---------------------------------------------------------------------------
# T3 — expire_old_snapshots removes old snapshots
# ---------------------------------------------------------------------------


def test_expire_old_snapshots_removes_old_snapshots(
    maintenance_table: Table,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """T3: 3 appends → expire with retention_days=0 → at least 2 removed."""
    log, sink = capturing_log_service
    _append_rows(maintenance_table, count=3)
    maintenance_table.refresh()

    sink.clear()
    result = expire_old_snapshots(maintenance_table, retention_days=0, log_service=log)

    assert isinstance(result, ExpireSnapshotsResult)
    assert result.snapshots_removed >= 2

    maintenance_table.refresh()
    current = maintenance_table.current_snapshot()
    assert current is not None
    assert result.latest_snapshot_id == current.snapshot_id

    expired_logs = [r for r in sink.records if r.message == 'platform.lake.snapshots_expired']
    assert len(expired_logs) == 1
    assert expired_logs[0].level == LogLevel.INFO


# ---------------------------------------------------------------------------
# T4 — expire_old_snapshots is idempotent when nothing is old enough
# ---------------------------------------------------------------------------


def test_expire_old_snapshots_is_idempotent_when_nothing_expired(
    maintenance_table: Table,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """T4: large retention window → zero snapshots removed, no exception."""
    log, sink = capturing_log_service
    _append_rows(maintenance_table, count=1)
    maintenance_table.refresh()

    sink.clear()
    result = expire_old_snapshots(maintenance_table, retention_days=3650, log_service=log)

    assert result.snapshots_removed == 0

    expired_logs = [r for r in sink.records if r.message == 'platform.lake.snapshots_expired']
    assert len(expired_logs) == 1


# ---------------------------------------------------------------------------
# T5 — clean_orphan_files removes old orphan
# ---------------------------------------------------------------------------


def test_clean_orphan_files_removes_old_orphan(
    maintenance_table: Table,
    capturing_log_service: tuple[LogService, CapturingLogSink],
    tmp_path: Path,
) -> None:
    """T5: orphan file older than guard → removed; referenced file stays."""
    log, sink = capturing_log_service
    _append_rows(maintenance_table, count=1)
    maintenance_table.refresh()

    # Locate the data directory.
    location = maintenance_table.location().rstrip('/')
    parsed = urllib.parse.urlparse(location)
    data_dir = Path(parsed.path) / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)

    # Drop an orphan file.
    orphan_content = b'fake parquet content for orphan test'
    orphan_path = data_dir / f'orphan-{uuid.uuid4()}.parquet'
    orphan_path.write_bytes(orphan_content)

    # Set mtime to 48 hours ago.
    old_ts = (datetime.now(tz=UTC) - timedelta(hours=48)).timestamp()
    os.utime(str(orphan_path), (old_ts, old_ts))

    sink.clear()
    result = clean_orphan_files(maintenance_table, older_than_hours=24, log_service=log)

    assert isinstance(result, CleanOrphanFilesResult)
    assert result.files_removed == 1
    assert result.bytes_freed == len(orphan_content)
    assert not orphan_path.exists(), 'orphan file should be deleted'

    # Referenced file still on disk.
    files_table = maintenance_table.inspect.files()
    for row in files_table.to_pylist():
        ref_path = Path(urllib.parse.urlparse(row['file_path']).path)
        assert ref_path.exists(), f'referenced file {ref_path} should still exist'

    cleaned_logs = [r for r in sink.records if r.message == 'platform.lake.orphan_files_cleaned']
    assert len(cleaned_logs) == 1
    assert cleaned_logs[0].level == LogLevel.INFO


# ---------------------------------------------------------------------------
# T6 — clean_orphan_files respects the safety window
# ---------------------------------------------------------------------------


def test_clean_orphan_files_respects_safety_window(
    maintenance_table: Table,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """T6: orphan file within the guard window → not removed."""
    log, sink = capturing_log_service
    _append_rows(maintenance_table, count=1)
    maintenance_table.refresh()

    location = maintenance_table.location().rstrip('/')
    parsed = urllib.parse.urlparse(location)
    data_dir = Path(parsed.path) / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)

    orphan_content = b'recent orphan content'
    orphan_path = data_dir / f'orphan-{uuid.uuid4()}.parquet'
    orphan_path.write_bytes(orphan_content)

    # Set mtime to 1 hour ago (within the 24h guard).
    recent_ts = (datetime.now(tz=UTC) - timedelta(hours=1)).timestamp()
    os.utime(str(orphan_path), (recent_ts, recent_ts))

    sink.clear()
    result = clean_orphan_files(maintenance_table, older_than_hours=24, log_service=log)

    assert result.files_removed == 0
    assert orphan_path.exists(), 'recent orphan should be left on disk'


# ---------------------------------------------------------------------------
# T7 — expire_old_snapshots failure path
# ---------------------------------------------------------------------------


def test_expire_old_snapshots_raises_lake_maintenance_error_on_failure(
    maintenance_table: Table,
    capturing_log_service: tuple[LogService, CapturingLogSink],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T7: monkeypatched commit() raises → LakeMaintenanceError with chained cause."""
    log, sink = capturing_log_service
    _append_rows(maintenance_table, count=1)
    maintenance_table.refresh()

    from pyiceberg.table.update.snapshot import ExpireSnapshots as _IceExpireSnapshots

    def _boom(self: object) -> None:
        raise RuntimeError('boom')

    monkeypatch.setattr(_IceExpireSnapshots, 'commit', _boom)

    sink.clear()
    with pytest.raises(LakeMaintenanceError) as excinfo:
        expire_old_snapshots(maintenance_table, log_service=log)

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert 'boom' in str(excinfo.value.__cause__)

    failed_logs = [r for r in sink.records if r.message == 'platform.lake.snapshots_expire_failed']
    assert len(failed_logs) == 1
    assert failed_logs[0].level == LogLevel.ERROR
