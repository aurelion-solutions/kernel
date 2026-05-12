# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EXECUTOR_HEARTBEAT_SECONDS loading and clamp in platform_executor_node/main.py."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.runtimes.platform_executor_node.main import _load_heartbeat_interval


def _make_log_spy() -> tuple[MagicMock, list[str]]:
    mock_log = MagicMock(spec=LogService)
    warnings: list[str] = []

    def _emit_safe(level: LogLevel, message: str, *, component: str, payload: dict, **kw: object) -> None:
        if level == LogLevel.WARNING:
            warnings.append(message)

    mock_log.emit_safe.side_effect = _emit_safe
    return mock_log, warnings


class TestLoadHeartbeatInterval:
    def test_default_when_env_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No env var → default 60.0s, no warning."""
        monkeypatch.delenv('EXECUTOR_HEARTBEAT_SECONDS', raising=False)
        mock_log, warnings = _make_log_spy()

        value = _load_heartbeat_interval(mock_log)
        assert value == 60.0
        assert warnings == []

    def test_valid_value_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid env var >= 1.0 → returned as-is, no warning."""
        monkeypatch.setenv('EXECUTOR_HEARTBEAT_SECONDS', '10')
        mock_log, warnings = _make_log_spy()

        value = _load_heartbeat_interval(mock_log)
        assert value == 10.0
        assert warnings == []

    def test_sub_one_clamped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Value < 1.0 → clamped to 1.0 with WARNING."""
        monkeypatch.setenv('EXECUTOR_HEARTBEAT_SECONDS', '0.1')
        mock_log, warnings = _make_log_spy()

        value = _load_heartbeat_interval(mock_log)
        assert value == 1.0
        assert len(warnings) == 1
        assert 'clamped' in warnings[0].lower()

    def test_non_numeric_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Non-numeric env value → default 60.0, no warning."""
        monkeypatch.setenv('EXECUTOR_HEARTBEAT_SECONDS', 'not-a-number')
        mock_log, warnings = _make_log_spy()

        value = _load_heartbeat_interval(mock_log)
        assert value == 60.0
        assert warnings == []

    def test_exactly_one_second_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Value == 1.0 → accepted without clamping."""
        monkeypatch.setenv('EXECUTOR_HEARTBEAT_SECONDS', '1.0')
        mock_log, warnings = _make_log_spy()

        value = _load_heartbeat_interval(mock_log)
        assert value == 1.0
        assert warnings == []
