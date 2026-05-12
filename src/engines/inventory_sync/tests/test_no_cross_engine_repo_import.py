# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Slice-local invariant guard — inventory_sync must not import from inventory_reconcile.repository.

Enforces the fix landed in Phase 18 Step 9d:
  ``bulk_approve_run_pending_items`` was relocated from ``inventory_reconcile.repository``
  to ``inventory_sync.repository``. This test ensures the cross-engine repository import
  never creeps back.

Scans ``src/engines/inventory_sync/**/*.py`` (excluding ``tests/`` and ``__pycache__/``)
for ``from src.engines.inventory_reconcile.repository import``.

Pattern is split at runtime so this file itself does not match the scanner.
A future broader AST guard (ARCH_CONTEXT line 375) will supersede this check.
"""

from __future__ import annotations

from pathlib import Path
import re

# Split the forbidden module path at runtime so this file itself does not match.
_FORBIDDEN_MODULE = 'src.engines.' + 'inventory_reconcile.repository'

_IMPORT_PATTERN = re.compile(
    r'^\s*from\s+' + re.escape(_FORBIDDEN_MODULE) + r'\s+import\b',
    re.MULTILINE,
)

_SYNC_APPLY_ROOT = Path(__file__).parent.parent


def test_no_cross_engine_repo_import() -> None:
    """inventory_sync source files must not import from inventory_reconcile.repository."""
    violations: list[str] = []

    for py_file in sorted(_SYNC_APPLY_ROOT.rglob('*.py')):
        # Exclude tests/ and __pycache__/
        if 'tests' in py_file.parts or '__pycache__' in py_file.parts:
            continue

        source = py_file.read_text(encoding='utf-8')
        if _IMPORT_PATTERN.search(source):
            violations.append(str(py_file.relative_to(_SYNC_APPLY_ROOT)))

    assert not violations, (
        'inventory_sync source files import from inventory_reconcile.repository '
        '(cross-engine repository import is forbidden per ARCH_CONTEXT line 374):\n'
        + '\n'.join(f'  {v}' for v in violations)
    )
