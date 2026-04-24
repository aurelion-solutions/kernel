# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the _SEED_ROWS constant in the seed migration.

The migration filename starts with a digit, so it cannot be imported with a plain
``import`` statement. Use ``importlib.import_module`` instead.
"""

from __future__ import annotations

import importlib
import re


def _load_seed_rows() -> list[dict]:
    mod = importlib.import_module('ops.db_versions.2026_04_24_0800_seed_capability_scope_keys')
    return mod._SEED_ROWS  # type: ignore[attr-defined]


def test_seed_rows_has_exactly_seventeen_codes() -> None:
    """_SEED_ROWS must contain exactly 17 entries with valid codes, names, and created_by."""
    rows = _load_seed_rows()

    assert len(rows) == 17, f'Expected 17 rows, got {len(rows)}'

    codes = [row['code'] for row in rows]
    assert len(set(codes)) == len(codes), f'Duplicate codes found: {codes}'

    code_pattern = re.compile(r'^[A-Z][A-Z0-9_]*$')
    for row in rows:
        assert code_pattern.match(row['code']), f'Invalid code format: {row["code"]}'
        assert row['name'], f'Empty name for code {row["code"]}'
        assert row['created_by'] == 'system:phase_13_seed', (
            f'Unexpected created_by for code {row["code"]}: {row["created_by"]}'
        )


def test_seed_rows_contains_required_codes() -> None:
    """_SEED_ROWS must contain at minimum GLOBAL and LEGAL_ENTITY."""
    rows = _load_seed_rows()
    codes = {row['code'] for row in rows}
    assert 'GLOBAL' in codes
    assert 'LEGAL_ENTITY' in codes
