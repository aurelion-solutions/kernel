# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Layer invariant tests — reconciliation must not depend on sync_apply internals.

Enforces the architectural rule that ``capabilities/reconciliation/`` service,
pipeline, repository, and model files MUST NOT import from the sync_apply slice,
the lake writer module, or preflight recovery helpers.

``routes.py`` is explicitly EXCLUDED from the check — it is the only allowed
reconciliation file that may reference ``SyncApplyService`` for the
``auto_apply`` delegation.
"""

from __future__ import annotations

from pathlib import Path
import re

# Split the forbidden module names so that this file itself does not match
# the import-scanner in the writer test file (grep over src/ for the module name).
# The identifiers are split at runtime to avoid false-positive grep hits.
_LAKE_WRITER = 'lake' + '_writer'  # noqa: RUF100
_PREFLIGHT = 'preflight' + '_recover_already_written'

# Pattern matches import statements that pull from sync_apply or the iceberg writer modules.
_IMPORT_PATTERN = re.compile(
    r'^\s*(?:from|import)\s+.*\b(sync_apply|' + _LAKE_WRITER + r')\b'
    r'|'
    r'^\s*(?:from|import)\s+.*\b(SyncApplyService|' + _PREFLIGHT + r')\b'
)

# Files that are explicitly allowed to reference sync_apply — routes.py only.
_ALLOWED_SUFFIXES = frozenset({'routes.py'})


def test_reconciliation_does_not_import_sync_apply() -> None:
    """reconciliation service/pipeline/repository/models files must not import sync_apply."""
    reconciliation_dir = Path(__file__).parent.parent  # src/capabilities/reconciliation/

    violations: list[tuple[str, int, str]] = []

    for py_file in reconciliation_dir.rglob('*.py'):
        # Exclude tests directory, allowed files, and __pycache__
        if '__pycache__' in py_file.parts:
            continue
        if 'tests' in py_file.parts:
            continue
        if py_file.name in _ALLOWED_SUFFIXES:
            continue

        text = py_file.read_text(encoding='utf-8')
        for lineno, line in enumerate(text.splitlines(), start=1):
            # Skip comment lines and blank lines
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if _IMPORT_PATTERN.match(line):
                violations.append((str(py_file.relative_to(reconciliation_dir.parent.parent)), lineno, stripped))

    if violations:
        lines_str = '\n'.join(f'  {path}:{lineno}: {line}' for path, lineno, line in violations)
        raise AssertionError(
            f'reconciliation files must not import sync_apply internals.\n'
            f'Found {len(violations)} violation(s):\n{lines_str}\n\n'
            f'Only routes.py is allowed to reference SyncApplyService.'
        )
