# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Meta-test: no engine or inventory module performs direct DML against
orchestrator tables (pipeline_runs, step_runs, pipeline_event_waiters).

Scope:
- Checked: src/engines/**/*.py, src/inventory/**/*.py
- Excluded from violations:
    * src/platform/orchestrator/ (the sole-writer itself)
    * tests/ subdirectories within engines/inventory (fixture code may
      seed rows by calling OrchestratorService — that is expected and allowed)
    * aurelion-kernel/pipelines/ (YAML pipeline definitions, not Python)

Method:
    1. Grep for table names co-occurring with DML keywords (INSERT, UPDATE,
       DELETE, text() calls) in the same file — fast first pass.
    2. AST confirmation: check whether the matching lines contain any of the
       table name strings in a string literal or keyword argument context.
       Not a full SQL parser — string match on the line is sufficient.

Expected match count outside src/platform/orchestrator/: 0.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import re

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parents[5]  # aurelion-kernel/
_KERNEL_SRC = _REPO_ROOT / 'src'

_ORCHESTRATOR_TABLES = frozenset({'pipeline_runs', 'step_runs', 'pipeline_event_waiters'})

_DML_PATTERN = re.compile(r'\b(INSERT|UPDATE|DELETE|text\()\b', re.IGNORECASE)

# Directories to scan.
_SCAN_DIRS = [
    _KERNEL_SRC / 'engines',
    _KERNEL_SRC / 'inventory',
]

# Table name regex: catches table names as string literals in Python source.
_TABLE_PATTERN = re.compile(
    r'(?:pipeline_runs|step_runs|pipeline_event_waiters)',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------


def _collect_python_files(root: Path) -> list[Path]:
    """Return all .py files under root, excluding test directories."""
    result: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip test directories — they may call OrchestratorService directly.
        dirnames[:] = [d for d in dirnames if d not in ('tests', '__pycache__', '.venv')]
        for fname in filenames:
            if fname.endswith('.py'):
                result.append(Path(dirpath) / fname)
    return result


def _file_has_dml_and_table(path: Path) -> list[str]:
    """Return lines that contain both a DML keyword and an orchestrator table name."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return []
    violations: list[str] = []
    for line in text.splitlines():
        if _DML_PATTERN.search(line) and _TABLE_PATTERN.search(line):
            violations.append(line.strip())
    return violations


def _ast_confirm_table_in_string(path: Path, table_names: frozenset[str]) -> list[str]:
    """AST pass: find string constants that contain orchestrator table names.

    Returns the string values that match (for diagnostic output).
    Catches SQLAlchemy text("..."), Table("pipeline_runs", ...), and
    raw SQL string literals.
    """
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except SyntaxError:
        return []

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            val = node.value
            for tname in table_names:
                if tname in val.lower():
                    found.append(val[:120])
    return found


# ---------------------------------------------------------------------------
# The test
# ---------------------------------------------------------------------------


def test_no_engine_or_inventory_module_writes_orchestrator_tables() -> None:
    """Assert that no engine/inventory Python module outside tests/ directly
    references orchestrator table names alongside DML operations."""
    all_files: list[Path] = []
    for scan_dir in _SCAN_DIRS:
        if scan_dir.exists():
            all_files.extend(_collect_python_files(scan_dir))

    violations: dict[str, list[str]] = {}

    for path in all_files:
        # Grep pass.
        grep_hits = _file_has_dml_and_table(path)
        if not grep_hits:
            continue
        # AST confirmation pass.
        ast_hits = _ast_confirm_table_in_string(path, _ORCHESTRATOR_TABLES)
        if ast_hits:
            rel = str(path.relative_to(_REPO_ROOT))
            violations[rel] = ast_hits

    if violations:
        lines = [
            'Orchestrator state-ownership violation: the following files perform',
            'direct DML against pipeline_runs / step_runs / pipeline_event_waiters.',
            'Only src/platform/orchestrator/service.py is allowed to write to these tables.',
            '',
        ]
        for file_path, strings in violations.items():
            lines.append(f'  {file_path}:')
            for s in strings:
                lines.append(f'    {s!r}')
        pytest.fail('\n'.join(lines))
