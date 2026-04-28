# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for run_compaction and _check_active_writes in service.py.

All tests use a MagicMock AsyncSession so that the suite never touches PG.
The safety-gate SQL queries are exercised by controlling what the mock
session.execute() returns.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from src.platform.lake.exceptions import LakeMaintenanceError
from src.platform.lake.maintenance import (
    CleanOrphanFilesResult,
    CompactTableResult,
    ExpireSnapshotsResult,
)
from src.platform.lake.service import run_compaction
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Canonical canned results
# ---------------------------------------------------------------------------

_COMPACT_RESULT = CompactTableResult(
    files_before=10,
    files_after=2,
    bytes_before=1_000_000,
    bytes_after=950_000,
    snapshot_id=42,
)
_EXPIRE_RESULT = ExpireSnapshotsResult(snapshots_removed=3, latest_snapshot_id=42)
_CLEAN_RESULT = CleanOrphanFilesResult(files_removed=5, bytes_freed=200_000)


# ---------------------------------------------------------------------------
# Session mock helpers
# ---------------------------------------------------------------------------


def _make_session(*, running_count: int = 0, batch_count: int = 0) -> MagicMock:
    """Return a mock AsyncSession whose execute() returns controlled scalar counts.

    Query order is fixed:
      1. sync_apply_runs count  (running_count)
      2. lake_batches count     (batch_count, only reached when running_count==0)
    """
    session = MagicMock()

    call_index: list[int] = [0]
    counts = [running_count, batch_count]

    async def _execute(stmt, *args, **kwargs):
        result = MagicMock()
        idx = call_index[0]
        result.scalar_one.return_value = counts[idx] if idx < len(counts) else 0
        call_index[0] += 1
        return result

    session.execute = _execute
    return session


def _make_catalog() -> MagicMock:
    """Return a MagicMock Catalog whose load_table() returns a Mock Table."""
    catalog = MagicMock()

    def _load(identifier: tuple[str, str]) -> MagicMock:
        tbl = MagicMock()
        tbl.name.return_value = identifier
        return tbl

    catalog.load_table.side_effect = _load
    return catalog


# ---------------------------------------------------------------------------
# T1: happy path — gate clear, all three functions called per table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_no_active_writes() -> None:
    """All three maintenance functions called; orphan_cleanup_skipped is False."""
    catalog = _make_catalog()
    session = _make_session(running_count=0, batch_count=0)

    with (
        patch('src.platform.lake.service.compact_table', return_value=_COMPACT_RESULT) as m_compact,
        patch('src.platform.lake.service.expire_old_snapshots', return_value=_EXPIRE_RESULT) as m_expire,
        patch('src.platform.lake.service.clean_orphan_files', return_value=_CLEAN_RESULT) as m_clean,
    ):
        response = await run_compaction(
            catalog,
            MagicMock(),
            session,
            table='all',
            retention_days=7,
            orphan_older_than_hours=24,
            target_file_size_mb=128,
            log_service=NoOpLogService(),
        )

    assert response.orphan_cleanup_skipped is False
    assert response.orphan_cleanup_skip_reason is None
    assert len(response.tables) == 2
    assert m_compact.call_count == 2
    assert m_expire.call_count == 2
    assert m_clean.call_count == 2

    for tbl in response.tables:
        assert tbl.files_before == 10
        assert tbl.files_after == 2
        assert tbl.bytes_before == 1_000_000
        assert tbl.bytes_after == 950_000
        assert tbl.compaction_snapshot_id == 42
        assert tbl.snapshots_removed == 3
        assert tbl.orphan_files_removed == 5
        assert tbl.orphan_bytes_freed == 200_000
        assert tbl.orphan_cleanup_skipped is False


# ---------------------------------------------------------------------------
# T2: gate blocked by running sync_apply_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_blocked_by_running_sync_apply_run() -> None:
    """clean_orphan_files NOT called when running_count > 0."""
    catalog = _make_catalog()
    session = _make_session(running_count=1, batch_count=0)

    with (
        patch('src.platform.lake.service.compact_table', return_value=_COMPACT_RESULT) as m_compact,
        patch('src.platform.lake.service.expire_old_snapshots', return_value=_EXPIRE_RESULT) as m_expire,
        patch('src.platform.lake.service.clean_orphan_files', return_value=_CLEAN_RESULT) as m_clean,
    ):
        response = await run_compaction(
            catalog,
            MagicMock(),
            session,
            table='all',
            retention_days=7,
            orphan_older_than_hours=24,
            target_file_size_mb=128,
            log_service=NoOpLogService(),
        )

    assert response.orphan_cleanup_skipped is True
    assert response.orphan_cleanup_skip_reason is not None
    assert 'running' in response.orphan_cleanup_skip_reason
    assert m_compact.call_count == 2
    assert m_expire.call_count == 2
    m_clean.assert_not_called()

    for tbl in response.tables:
        assert tbl.orphan_files_removed == 0
        assert tbl.orphan_cleanup_skipped is True
        assert tbl.orphan_cleanup_skip_reason is not None


# ---------------------------------------------------------------------------
# T3: gate blocked by recent lake_batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_blocked_by_recent_lake_batch() -> None:
    """clean_orphan_files NOT called when batch_count > 0 within the window."""
    catalog = _make_catalog()
    session = _make_session(running_count=0, batch_count=3)

    with (
        patch('src.platform.lake.service.compact_table', return_value=_COMPACT_RESULT),
        patch('src.platform.lake.service.expire_old_snapshots', return_value=_EXPIRE_RESULT),
        patch('src.platform.lake.service.clean_orphan_files', return_value=_CLEAN_RESULT) as m_clean,
    ):
        response = await run_compaction(
            catalog,
            MagicMock(),
            session,
            table='all',
            retention_days=7,
            orphan_older_than_hours=24,
            target_file_size_mb=128,
            log_service=NoOpLogService(),
        )

    assert response.orphan_cleanup_skipped is True
    assert response.orphan_cleanup_skip_reason is not None
    assert 'lake_batches' in response.orphan_cleanup_skip_reason
    m_clean.assert_not_called()


# ---------------------------------------------------------------------------
# T4: maintenance error propagates from compact_table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maintenance_error_propagates() -> None:
    """LakeMaintenanceError raised by compact_table propagates out of run_compaction."""
    catalog = _make_catalog()
    session = _make_session(running_count=0, batch_count=0)

    with patch('src.platform.lake.service.compact_table', side_effect=LakeMaintenanceError('boom')):
        with pytest.raises(LakeMaintenanceError, match='boom'):
            await run_compaction(
                catalog,
                MagicMock(),
                session,
                table='raw.access_artifacts',
                retention_days=7,
                orphan_older_than_hours=24,
                target_file_size_mb=128,
                log_service=NoOpLogService(),
            )


# ---------------------------------------------------------------------------
# T5: single table target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_target_single_table() -> None:
    """table='raw.access_artifacts' → exactly one result, namespace 'raw'."""
    catalog = _make_catalog()
    session = _make_session(running_count=0, batch_count=0)

    with (
        patch('src.platform.lake.service.compact_table', return_value=_COMPACT_RESULT),
        patch('src.platform.lake.service.expire_old_snapshots', return_value=_EXPIRE_RESULT),
        patch('src.platform.lake.service.clean_orphan_files', return_value=_CLEAN_RESULT) as m_clean,
    ):
        response = await run_compaction(
            catalog,
            MagicMock(),
            session,
            table='raw.access_artifacts',
            retention_days=7,
            orphan_older_than_hours=24,
            target_file_size_mb=128,
            log_service=NoOpLogService(),
        )

    assert len(response.tables) == 1
    assert response.tables[0].namespace == 'raw'
    assert response.tables[0].name == 'access_artifacts'
    m_clean.assert_called_once()
