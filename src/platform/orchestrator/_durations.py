# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared duration parsing for the pipeline orchestrator.

Package-private (leading underscore).  Imported by both ``runner.py`` and
``beat.py`` so the grammar lives in exactly one place.
"""

from __future__ import annotations

from datetime import timedelta
import re

# Matches duration strings like "30s", "5m", "2h", "7d".
_DURATION_RE = re.compile(r'^(\d+)(s|m|h|d)$')

# Multiplier table for _parse_duration.
_DURATION_MULTIPLIERS: dict[str, int] = {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}


def parse_duration(s: str) -> timedelta:
    """Parse '30s' / '5m' / '2h' / '7d' into a timedelta.

    Raises ValueError on empty / malformed / zero / unit-less input.
    Accepted suffixes: s (seconds), m (minutes), h (hours), d (days).
    """
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(f'invalid duration: {s!r}')
    value = int(m.group(1))
    if value == 0:
        raise ValueError(f'duration must be > 0, got: {s!r}')
    unit = m.group(2)
    return timedelta(seconds=value * _DURATION_MULTIPLIERS[unit])
