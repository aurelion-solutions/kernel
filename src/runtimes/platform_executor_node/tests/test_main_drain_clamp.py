# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for drain timeout loading and clamp logic in platform_executor_node/main.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Import the function under test at module level (no process-level side effects).
# We use importlib-style isolation because main.py registers module-level providers.
from src.runtimes.platform_executor_node.main import _load_drain_timeout


def _make_log_spy() -> tuple[MagicMock, list[str]]:
    """Return a mock LogService and a list that collects WARNING messages."""
    mock_log = MagicMock(spec=LogService)
    warnings: list[str] = []

    def _emit_safe(level: LogLevel, message: str, *, component: str, payload: dict[str, object], **kw: object) -> None:
        if level == LogLevel.WARNING:
            warnings.append(message)

    mock_log.emit_safe.side_effect = _emit_safe
    return mock_log, warnings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLoadDrainTimeout:
    def test_default_when_env_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env var → default 60s returned, no warning."""
        monkeypatch.delenv('EXECUTOR_DRAIN_TIMEOUT_SECONDS', raising=False)
        mock_log, warnings = _make_log_spy()

        value = _load_drain_timeout(mock_log)
        assert value == 60.0
        assert warnings == []

    def test_valid_value_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid env var above threshold → returned as-is, no warning."""
        monkeypatch.setenv('EXECUTOR_DRAIN_TIMEOUT_SECONDS', '120')
        mock_log, warnings = _make_log_spy()

        value = _load_drain_timeout(mock_log)
        assert value == 120.0
        assert warnings == []

    def test_clamp_when_too_low(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Value < threshold + 5 → clamped, WARNING logged."""
        monkeypatch.setenv('EXECUTOR_DRAIN_TIMEOUT_SECONDS', '5')  # below 15
        mock_log, warnings = _make_log_spy()

        value = _load_drain_timeout(mock_log)
        # Must be clamped to _RECLAIM_STALE_THRESHOLD_SECONDS + 5 = 15.
        assert value == 15.0
        assert len(warnings) == 1
        assert 'clamped' in warnings[0].lower()

    def test_clamp_at_exact_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Value == threshold (10s) → clamped to 15s with WARNING."""
        monkeypatch.setenv('EXECUTOR_DRAIN_TIMEOUT_SECONDS', '10')
        mock_log, warnings = _make_log_spy()

        value = _load_drain_timeout(mock_log)
        assert value == 15.0
        assert len(warnings) == 1

    def test_clamp_exactly_at_min_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Value == threshold + 5 = 15s → accepted without clamping."""
        monkeypatch.setenv('EXECUTOR_DRAIN_TIMEOUT_SECONDS', '15')
        mock_log, warnings = _make_log_spy()

        value = _load_drain_timeout(mock_log)
        assert value == 15.0
        assert warnings == []

    def test_invalid_env_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-numeric env var → falls back to default 60s."""
        monkeypatch.setenv('EXECUTOR_DRAIN_TIMEOUT_SECONDS', 'not-a-number')
        mock_log, warnings = _make_log_spy()

        value = _load_drain_timeout(mock_log)
        assert value == 60.0
