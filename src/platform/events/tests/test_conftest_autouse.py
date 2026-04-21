# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke test: session-scoped autouse fixture pins AURELION_EVENTS_PROVIDER=noop."""

import os


def test_events_provider_is_noop() -> None:
    """The conftest autouse fixture must set AURELION_EVENTS_PROVIDER=noop for the whole suite."""
    assert os.environ.get('AURELION_EVENTS_PROVIDER') == 'noop'
