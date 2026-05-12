# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Grep-based invariant guard: pipeline.py and repository.py must not mutate access_facts.

Scans specific Step-8 files that should never import or call AccessFactService
or individual fact mutation methods.

Files checked (Step 8 scope):
  - pipeline.py
  - repository.py
  - views.py

Files explicitly NOT checked here (Step 9 scope — still under migration):
  - service.py  — Step 9 will remove AccessFactService dependency
  - deps.py     — Step 9 will wire to new pipeline
  - routes.py   — Step 9 will update

This test protects against future regressions where someone accidentally
re-introduces a direct fact-mutation call into the pipeline.

Note: comments and docstrings that *mention* these symbols are allowed;
the check is against ``import`` statements and call expressions only.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Step-8 owned files — must not contain fact-mutation symbols
_STEP8_FILES = ('pipeline.py', 'repository.py', 'views.py')

# Forbidden: import names and attribute-access call targets
_FORBIDDEN_NAMES = frozenset(
    {
        'create_fact',
        'revoke_fact',
        'refresh_fact_fields',
        'AccessFactService',
    }
)


def _get_step8_files() -> list[Path]:
    base = Path(__file__).parent.parent  # src/engines/inventory_reconcile/
    return [base / fname for fname in _STEP8_FILES if (base / fname).exists()]


def _extract_imported_names(source: str) -> set[str]:
    """Return all names imported in the module (import and from-import)."""
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split('.')[0])
    return imported


def _extract_called_names(source: str) -> set[str]:
    """Return all names and attribute names used in Call nodes."""
    tree = ast.parse(source)
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return called


def test_no_access_fact_mutation_in_pipeline_and_repository() -> None:
    """Zero forbidden fact-mutation symbols in Step-8 files (pipeline.py, repository.py, views.py)."""
    files = _get_step8_files()
    assert files, 'Step-8 files not found — check test path'

    violations: list[str] = []
    for path in files:
        source = path.read_text(encoding='utf-8')
        imported = _extract_imported_names(source)
        called = _extract_called_names(source)
        used = imported | called
        for symbol in _FORBIDDEN_NAMES:
            if symbol in used:
                violations.append(f'{path.name}: uses forbidden symbol {symbol!r}')

    assert not violations, 'Step-8 reconciliation files contain forbidden fact-mutation symbols:\n' + '\n'.join(
        violations
    )
