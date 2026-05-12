# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for E5 stale apply lease cleanup action.

All tests use mocked DB session — no live database required.
Covers:
- Terminal status (completed/failed/cancelled) → row deleted
- Not found (None status) → row deleted (orphaned lease)
- Non-terminal status + not stale → row retained
- Non-terminal status + stale by timeout → row deleted with WARN logic
- Rows inspected / deleted counts in result
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.engines.access_plan.actions import (
    CleanupStaleApplyLeasesResult,
    cleanup_stale_apply_leases,
)
from src.engines.access_plan.models import AccessApplyActive
from src.platform.orchestrator.models import PipelineRunStatus
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lease(
    subject_ref: str | None = None,
    pipeline_run_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
) -> AccessApplyActive:
    row = MagicMock(spec=AccessApplyActive)
    row.subject_ref = subject_ref or f'subj-{uuid.uuid4()}'
    row.pipeline_run_id = pipeline_run_id or uuid.uuid4()
    row.started_at = started_at or datetime.now(UTC)
    return row


def _make_session(rows: list[AccessApplyActive]) -> AsyncMock:
    """Return an AsyncMock session that yields *rows* on SELECT AccessApplyActive."""
    session = AsyncMock()
    # First call: select AccessApplyActive (scalar list)
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    select_result = MagicMock()
    select_result.scalars.return_value = scalars_mock

    # Subsequent execute calls (get_pipeline_run_status + deletes) are handled separately
    # We track call order via side_effect
    session.execute = AsyncMock(return_value=select_result)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_rows_returns_zero_counts() -> None:
    """Empty table → no-op, result zeros."""
    session = _make_session([])
    result = await cleanup_stale_apply_leases(session)
    assert isinstance(result, CleanupStaleApplyLeasesResult)
    assert result.rows_inspected == 0
    assert result.rows_deleted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'terminal_status',
    [
        PipelineRunStatus.completed,
        PipelineRunStatus.failed,
        PipelineRunStatus.failed_timeout,
        PipelineRunStatus.cancelled,
    ],
)
async def test_terminal_status_deletes_row(terminal_status: PipelineRunStatus) -> None:
    """Lease whose pipeline run reached a terminal state is deleted."""
    lease = _make_lease()

    with patch(
        'src.engines.access_plan.actions.get_pipeline_run_status',
        new=AsyncMock(return_value=terminal_status),
    ):
        session = _make_session([lease])
        result = await cleanup_stale_apply_leases(session)

    assert result.rows_inspected == 1
    assert result.rows_deleted == 1


@pytest.mark.asyncio
async def test_none_status_deletes_orphaned_row() -> None:
    """Pipeline run row not found (None) → orphaned lease deleted."""
    lease = _make_lease()

    with patch(
        'src.engines.access_plan.actions.get_pipeline_run_status',
        new=AsyncMock(return_value=None),
    ):
        session = _make_session([lease])
        result = await cleanup_stale_apply_leases(session)

    assert result.rows_inspected == 1
    assert result.rows_deleted == 1


@pytest.mark.asyncio
async def test_running_not_stale_keeps_row() -> None:
    """Running pipeline, started recently → lease retained."""
    now = datetime.now(UTC)
    lease = _make_lease(started_at=now - timedelta(minutes=5))

    with patch(
        'src.engines.access_plan.actions.get_pipeline_run_status',
        new=AsyncMock(return_value=PipelineRunStatus.running),
    ):
        session = _make_session([lease])
        settings = RuntimeSettingsConfig(max_apply_duration_seconds=3600)
        result = await cleanup_stale_apply_leases(session, settings=settings, now=now)

    assert result.rows_inspected == 1
    assert result.rows_deleted == 0


@pytest.mark.asyncio
async def test_running_stale_by_timeout_deletes_row() -> None:
    """Running pipeline but started more than max_apply_duration ago → deleted."""
    now = datetime.now(UTC)
    max_secs = 3600
    lease = _make_lease(started_at=now - timedelta(seconds=max_secs + 1))

    with patch(
        'src.engines.access_plan.actions.get_pipeline_run_status',
        new=AsyncMock(return_value=PipelineRunStatus.running),
    ):
        session = _make_session([lease])
        settings = RuntimeSettingsConfig(max_apply_duration_seconds=max_secs)
        result = await cleanup_stale_apply_leases(session, settings=settings, now=now)

    assert result.rows_inspected == 1
    assert result.rows_deleted == 1


@pytest.mark.asyncio
async def test_mixed_rows_counts() -> None:
    """Three rows: one terminal, one not-found, one fresh running → 2 deleted."""
    now = datetime.now(UTC)
    run_terminal = uuid.uuid4()
    run_notfound = uuid.uuid4()
    run_running = uuid.uuid4()

    lease_terminal = _make_lease(pipeline_run_id=run_terminal)
    lease_notfound = _make_lease(pipeline_run_id=run_notfound)
    lease_running = _make_lease(pipeline_run_id=run_running, started_at=now - timedelta(minutes=2))

    status_map: dict[uuid.UUID, PipelineRunStatus | None] = {
        run_terminal: PipelineRunStatus.completed,
        run_notfound: None,
        run_running: PipelineRunStatus.running,
    }

    async def _mock_get_status(session: object, run_id: uuid.UUID) -> PipelineRunStatus | None:  # noqa: ARG001
        return status_map[run_id]

    with patch('src.engines.access_plan.actions.get_pipeline_run_status', new=_mock_get_status):
        session = _make_session([lease_terminal, lease_notfound, lease_running])
        settings = RuntimeSettingsConfig(max_apply_duration_seconds=3600)
        result = await cleanup_stale_apply_leases(session, settings=settings, now=now)

    assert result.rows_inspected == 3
    assert result.rows_deleted == 2


@pytest.mark.asyncio
async def test_naive_datetime_handled_as_utc() -> None:
    """started_at without tzinfo is treated as UTC for stale threshold comparison."""
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    max_secs = 3600
    # Naive UTC datetime (no tzinfo), old enough to be stale when interpreted as UTC
    naive_started_at = datetime(2026, 1, 1, 10, 0, 0)  # 2h before 'now', threshold is 1h
    lease = _make_lease(started_at=naive_started_at)

    with patch(
        'src.engines.access_plan.actions.get_pipeline_run_status',
        new=AsyncMock(return_value=PipelineRunStatus.running),
    ):
        session = _make_session([lease])
        settings = RuntimeSettingsConfig(max_apply_duration_seconds=max_secs)
        result = await cleanup_stale_apply_leases(session, settings=settings, now=now)

    assert result.rows_deleted == 1
