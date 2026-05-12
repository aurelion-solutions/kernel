# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for Phase 19 E4 — scheduled replan scanner.

Covers:
- Scanner finds initiative in window → emits subject.replan.required
- Scanner emits correct idempotency_key = sha1(subject_ref:window_bucket)
- Duplicate subjects within same scan → deduplicated (one event per subject)
- Scanner with no initiatives in window → zero subjects_queued
- window_lookback override via DI (tests use short lookback)
- window_bucket dedup: same subject_ref + bucket → same idempotency_key
- Scanner skips initiatives where subject_ref is None
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.inventory.initiatives.actions import (
    ScanForReplanResult,
    _build_idempotency_key,
    _window_bucket,
    run_scan_for_replan,
)
from src.platform.events.schemas import EventEnvelope
from src.platform.events.testing import CapturingEventService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_initiative(
    subject_ref: str | None = None,
    subject_type: str | None = 'employee',
    *,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.access_fact_id = uuid.uuid4()
    row.subject_ref = subject_ref or str(uuid.uuid4())
    row.subject_type = subject_type
    row.valid_from = valid_from or datetime.now(UTC)
    row.valid_until = valid_until
    return row


def _make_ctx(
    initiatives: list[MagicMock],
    event_service: CapturingEventService | None = None,
) -> MagicMock:
    """Build a minimal ActionContext mock with a session that returns given initiatives."""
    ctx = MagicMock()
    ctx.log_service = MagicMock()

    # Attach event_service so run_scan_for_replan finds it
    if event_service is not None:
        from src.platform.events.service import EventService  # noqa: PLC0415

        ctx.event_service = EventService(sink=event_service)
    else:
        ctx.event_service = None

    # Session mock: scan_for_replan_window is patched separately
    ctx.session = AsyncMock()
    return ctx


# ---------------------------------------------------------------------------
# Window bucket helpers
# ---------------------------------------------------------------------------


def test_window_bucket_stable_within_minute() -> None:
    """Two timestamps in the same minute share the same bucket."""
    base = datetime(2026, 5, 12, 10, 30, 0, tzinfo=UTC)
    t1 = base.replace(second=0)
    t2 = base.replace(second=59)
    assert _window_bucket(t1) == _window_bucket(t2)


def test_window_bucket_changes_across_minute() -> None:
    """Timestamps in different minutes have different buckets."""
    t1 = datetime(2026, 5, 12, 10, 30, 0, tzinfo=UTC)
    t2 = datetime(2026, 5, 12, 10, 31, 0, tzinfo=UTC)
    assert _window_bucket(t1) != _window_bucket(t2)


def test_idempotency_key_stable_for_same_input() -> None:
    """Same subject_ref + bucket → same idempotency_key (deterministic)."""
    key1 = _build_idempotency_key('subj-abc', 1234567)
    key2 = _build_idempotency_key('subj-abc', 1234567)
    assert key1 == key2


def test_idempotency_key_different_bucket() -> None:
    """Different bucket → different idempotency_key."""
    key1 = _build_idempotency_key('subj-abc', 1234567)
    key2 = _build_idempotency_key('subj-abc', 1234568)
    assert key1 != key2


def test_idempotency_key_different_subject() -> None:
    """Different subject_ref → different idempotency_key."""
    key1 = _build_idempotency_key('subj-abc', 1234567)
    key2 = _build_idempotency_key('subj-xyz', 1234567)
    assert key1 != key2


# ---------------------------------------------------------------------------
# Scanner behaviour tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scanner_finds_initiative_in_window() -> None:
    """Scanner finds one initiative → emits subject.replan.required."""
    now = datetime(2026, 5, 12, 10, 30, 30, tzinfo=UTC)
    subject_ref = str(uuid.uuid4())
    initiative = _make_initiative(subject_ref=subject_ref, subject_type='employee')
    capturing = CapturingEventService()
    ctx = _make_ctx([initiative], event_service=capturing)

    with patch(
        'src.inventory.initiatives.actions.scan_for_replan_window',
        new=AsyncMock(return_value=[initiative]),
    ):
        result = await run_scan_for_replan(ctx, now=now, lookback_seconds=120)

    assert isinstance(result, ScanForReplanResult)
    assert result.subjects_queued == 1
    assert result.initiatives_scanned == 1

    events = capturing.filter_by_type('subject.replan.required')
    assert len(events) == 1
    env = events[0]
    assert isinstance(env, EventEnvelope)
    assert env.payload['subject_id'] == subject_ref
    assert env.payload['subject_type'] == 'employee'
    assert 'idempotency_key' in env.payload
    assert 'window_bucket' in env.payload


@pytest.mark.asyncio
async def test_scanner_emits_correct_idempotency_key() -> None:
    """Emitted idempotency_key == sha1(subject_ref:window_bucket)."""
    now = datetime(2026, 5, 12, 10, 30, 30, tzinfo=UTC)
    subject_ref = 'test-subject-ref'
    initiative = _make_initiative(subject_ref=subject_ref)
    capturing = CapturingEventService()
    ctx = _make_ctx([initiative], event_service=capturing)

    expected_bucket = _window_bucket(now)
    expected_key = _build_idempotency_key(subject_ref, expected_bucket)

    with patch(
        'src.inventory.initiatives.actions.scan_for_replan_window',
        new=AsyncMock(return_value=[initiative]),
    ):
        await run_scan_for_replan(ctx, now=now, lookback_seconds=120)

    events = capturing.filter_by_type('subject.replan.required')
    assert events[0].payload['idempotency_key'] == expected_key
    assert events[0].payload['window_bucket'] == expected_bucket


@pytest.mark.asyncio
async def test_scanner_deduplicates_same_subject() -> None:
    """Two initiatives for same subject → only one event emitted."""
    now = datetime(2026, 5, 12, 10, 30, 30, tzinfo=UTC)
    subject_ref = str(uuid.uuid4())
    i1 = _make_initiative(subject_ref=subject_ref)
    i2 = _make_initiative(subject_ref=subject_ref)
    capturing = CapturingEventService()
    ctx = _make_ctx([i1, i2], event_service=capturing)

    with patch(
        'src.inventory.initiatives.actions.scan_for_replan_window',
        new=AsyncMock(return_value=[i1, i2]),
    ):
        result = await run_scan_for_replan(ctx, now=now, lookback_seconds=120)

    assert result.subjects_queued == 1
    assert result.initiatives_scanned == 2
    events = capturing.filter_by_type('subject.replan.required')
    assert len(events) == 1


@pytest.mark.asyncio
async def test_scanner_multiple_subjects_each_gets_event() -> None:
    """Three different subjects → three distinct events."""
    now = datetime(2026, 5, 12, 10, 30, 30, tzinfo=UTC)
    initiatives = [_make_initiative(subject_ref=str(uuid.uuid4())) for _ in range(3)]
    capturing = CapturingEventService()
    ctx = _make_ctx(initiatives, event_service=capturing)

    with patch(
        'src.inventory.initiatives.actions.scan_for_replan_window',
        new=AsyncMock(return_value=initiatives),
    ):
        result = await run_scan_for_replan(ctx, now=now, lookback_seconds=120)

    assert result.subjects_queued == 3
    events = capturing.filter_by_type('subject.replan.required')
    assert len(events) == 3
    subject_ids = {e.payload['subject_id'] for e in events}
    assert len(subject_ids) == 3


@pytest.mark.asyncio
async def test_scanner_no_initiatives_returns_zero() -> None:
    """Empty window → no events, zero subjects_queued."""
    now = datetime(2026, 5, 12, 10, 30, 30, tzinfo=UTC)
    capturing = CapturingEventService()
    ctx = _make_ctx([], event_service=capturing)

    with patch(
        'src.inventory.initiatives.actions.scan_for_replan_window',
        new=AsyncMock(return_value=[]),
    ):
        result = await run_scan_for_replan(ctx, now=now, lookback_seconds=120)

    assert result.subjects_queued == 0
    assert result.initiatives_scanned == 0
    events = capturing.filter_by_type('subject.replan.required')
    assert len(events) == 0


@pytest.mark.asyncio
async def test_scanner_skips_initiatives_without_subject_ref() -> None:
    """Initiatives with subject_ref=None are silently skipped."""
    now = datetime(2026, 5, 12, 10, 30, 30, tzinfo=UTC)
    # scan_for_replan_window already filters NULLs, but test defensive path
    i_null = _make_initiative(subject_ref=None)
    i_null.subject_ref = None  # force None past MagicMock default
    i_valid = _make_initiative(subject_ref=str(uuid.uuid4()))
    capturing = CapturingEventService()
    ctx = _make_ctx([i_null, i_valid], event_service=capturing)

    with patch(
        'src.inventory.initiatives.actions.scan_for_replan_window',
        new=AsyncMock(return_value=[i_null, i_valid]),
    ):
        result = await run_scan_for_replan(ctx, now=now, lookback_seconds=120)

    assert result.subjects_queued == 1
    events = capturing.filter_by_type('subject.replan.required')
    assert len(events) == 1
    assert events[0].payload['subject_id'] == i_valid.subject_ref


@pytest.mark.asyncio
async def test_scanner_window_lookback_override_via_di() -> None:
    """lookback_seconds DI override is used instead of RuntimeSettingsConfig default."""
    now = datetime(2026, 5, 12, 10, 30, 30, tzinfo=UTC)
    subject_ref = str(uuid.uuid4())
    initiative = _make_initiative(subject_ref=subject_ref)
    capturing = CapturingEventService()
    ctx = _make_ctx([initiative], event_service=capturing)

    captured_window_start: list[datetime] = []

    async def _spy_scan(session, *, window_start: datetime, window_end: datetime):  # type: ignore[no-untyped-def]
        captured_window_start.append(window_start)
        return [initiative]

    with patch('src.inventory.initiatives.actions.scan_for_replan_window', new=_spy_scan):
        await run_scan_for_replan(ctx, now=now, lookback_seconds=10)

    assert len(captured_window_start) == 1
    # With lookback=10s the window_start should be now - 10s
    expected_start = now - timedelta(seconds=10)
    assert abs((captured_window_start[0] - expected_start).total_seconds()) < 1


@pytest.mark.asyncio
async def test_dedupe_across_overlapping_runs_same_bucket() -> None:
    """Two scanner runs in the same minute produce the same idempotency_key."""
    t1 = datetime(2026, 5, 12, 10, 30, 15, tzinfo=UTC)
    t2 = datetime(2026, 5, 12, 10, 30, 45, tzinfo=UTC)
    # Same minute → same bucket
    assert _window_bucket(t1) == _window_bucket(t2)

    subject_ref = 'stable-subject'
    initiative = _make_initiative(subject_ref=subject_ref)

    keys: list[str] = []
    for now in (t1, t2):
        capturing = CapturingEventService()
        ctx = _make_ctx([initiative], event_service=capturing)
        with patch(
            'src.inventory.initiatives.actions.scan_for_replan_window',
            new=AsyncMock(return_value=[initiative]),
        ):
            await run_scan_for_replan(ctx, now=now, lookback_seconds=120)
        events = capturing.filter_by_type('subject.replan.required')
        keys.append(events[0].payload['idempotency_key'])

    assert keys[0] == keys[1], 'Same bucket should produce identical idempotency_key'
