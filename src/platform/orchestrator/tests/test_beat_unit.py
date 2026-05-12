# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for beat.py — no DB required."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from src.platform.orchestrator._durations import parse_duration
from src.platform.orchestrator.beat import BeatScheduleParseError, compute_previous_fire_point

# ---------------------------------------------------------------------------
# compute_previous_fire_point — cron
# ---------------------------------------------------------------------------


def test_cron_fire_at_03_returns_02_today() -> None:
    """cron '0 2 * * *' at 03:00 UTC → 02:00 UTC same day."""
    now = datetime(2026, 5, 10, 3, 0, 0, tzinfo=UTC)
    fp = compute_previous_fire_point(now, cron='0 2 * * *')
    expected = datetime(2026, 5, 10, 2, 0, 0, tzinfo=UTC)
    assert fp == expected


def test_cron_fire_at_01_30_returns_02_yesterday() -> None:
    """cron '0 2 * * *' at 01:30 UTC → 02:00 UTC previous day."""
    now = datetime(2026, 5, 10, 1, 30, 0, tzinfo=UTC)
    fp = compute_previous_fire_point(now, cron='0 2 * * *')
    expected = datetime(2026, 5, 9, 2, 0, 0, tzinfo=UTC)
    assert fp == expected


def test_cron_result_is_utc_aware() -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    fp = compute_previous_fire_point(now, cron='*/5 * * * *')
    assert fp.tzinfo is not None
    assert fp.utcoffset() == timedelta(0)


# ---------------------------------------------------------------------------
# compute_previous_fire_point — every
# ---------------------------------------------------------------------------


def test_every_5m_at_12_07_30_returns_12_05_00() -> None:
    """every='5m' at 12:07:30 → 12:05:00 (epoch-anchored)."""
    now = datetime(2026, 5, 10, 12, 7, 30, tzinfo=UTC)
    fp = compute_previous_fire_point(now, every='5m')
    expected = datetime(2026, 5, 10, 12, 5, 0, tzinfo=UTC)
    assert fp == expected


def test_every_1h_at_12_07_30_returns_12_00_00() -> None:
    """every='1h' at 12:07:30 → 12:00:00 (epoch-anchored)."""
    now = datetime(2026, 5, 10, 12, 7, 30, tzinfo=UTC)
    fp = compute_previous_fire_point(now, every='1h')
    expected = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    assert fp == expected


def test_every_result_is_utc_aware() -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    fp = compute_previous_fire_point(now, every='30s')
    assert fp.tzinfo is not None


# ---------------------------------------------------------------------------
# compute_previous_fire_point — errors
# ---------------------------------------------------------------------------


def test_malformed_cron_raises() -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(BeatScheduleParseError, match='invalid cron'):
        compute_previous_fire_point(now, cron='not-a-cron')


def test_malformed_every_raises() -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(BeatScheduleParseError, match='invalid every duration'):
        compute_previous_fire_point(now, every='bad-duration')


def test_both_cron_and_every_raises() -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(BeatScheduleParseError):
        compute_previous_fire_point(now, cron='* * * * *', every='5m')


def test_neither_cron_nor_every_raises() -> None:
    now = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    with pytest.raises(BeatScheduleParseError):
        compute_previous_fire_point(now)


# ---------------------------------------------------------------------------
# _parse_duration identity — same object as runner's symbol
# ---------------------------------------------------------------------------


def test_parse_duration_is_same_object_as_runner_symbol() -> None:
    """beat._durations.parse_duration is the canonical symbol; runner re-exports it."""
    from src.platform.orchestrator.runner import _parse_duration as runner_pd

    assert runner_pd is parse_duration, '_parse_duration in runner must be the same object as _durations.parse_duration'
