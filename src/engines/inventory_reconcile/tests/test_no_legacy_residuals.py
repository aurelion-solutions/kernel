# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Static grep guard — confirms no legacy role/privilege entity names survive in source."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


def _find_kernel_src_root() -> Path:
    """Locate aurelion-kernel/src/ by walking up to pyproject.toml marker."""
    p = Path(__file__).resolve().parent
    while p != p.parent:
        if (p / 'pyproject.toml').exists():
            return p / 'src'
        p = p.parent
    raise RuntimeError('Could not locate pyproject.toml — cannot determine kernel src root')


RECONCILIATION_SRC = Path(__file__).resolve().parent.parent
KERNEL_SRC_ROOT = _find_kernel_src_root()
_THIS_FILE = Path(__file__).resolve()


def test_no_role_privilege_residuals_in_reconciliation():
    """grep must find zero occurrences of legacy entity names in reconciliation source."""
    # Patterns that must not appear (entity names, FK columns)
    patterns = ['ent_roles', 'ent_privileges', 'role_id', 'privilege_id']

    for pattern in patterns:
        result = subprocess.run(
            ['grep', '-RIn', pattern, str(RECONCILIATION_SRC)],
            capture_output=True,
            text=True,
        )
        # Filter out __pycache__ and this test file itself
        hits = [
            line
            for line in result.stdout.splitlines()
            if '__pycache__' not in line and 'test_no_legacy_residuals' not in line
        ]
        assert hits == [], f'Legacy pattern {pattern!r} found in reconciliation source:\n' + '\n'.join(hits)


def test_no_legacy_role_privilege_residuals_in_src():
    """Phase 12 DoD: zero legacy role/privilege residuals anywhere in aurelion-kernel/src/.

    Scans all .py files under KERNEL_SRC_ROOT for table names ent_roles / ent_privileges
    and FK column names role_id / privilege_id. Excludes __pycache__ directories and this
    test file itself (which legitimately mentions the patterns as search targets).
    """
    forbidden = re.compile(r'\bent_roles\b|\bent_privileges\b|\brole_id\b|\bprivilege_id\b')
    hits: list[tuple[str, int, str]] = []

    for py_file in KERNEL_SRC_ROOT.rglob('*.py'):
        if '__pycache__' in py_file.parts:
            continue
        if py_file.resolve() == _THIS_FILE:
            # Skip self — this file mentions the patterns as grep targets
            continue
        # Skip sibling guard-test files (test_no_legacy_residuals.py in other slices)
        # that legitimately contain the same pattern strings as search targets.
        if py_file.name == 'test_no_legacy_residuals.py':
            continue
        content = py_file.read_text(encoding='utf-8')
        for lineno, line in enumerate(content.splitlines(), start=1):
            if forbidden.search(line):
                hits.append((str(py_file.relative_to(KERNEL_SRC_ROOT)), lineno, line.strip()))

    assert not hits, (
        'Phase 12 DoD violated — legacy role/privilege residuals found in aurelion-kernel/src/:\n'
        + '\n'.join(f'  {f}:{ln}: {txt}' for f, ln, txt in hits)
    )


def test_deleted_modules_no_longer_importable():
    """Legacy modules must raise ModuleNotFoundError."""
    import importlib

    import pytest

    for module_path in [
        'src.engines.inventory_reconcile.engine',
        'src.engines.inventory_reconcile.orchestrator',
        'src.engines.inventory_reconcile.reconciler_account',
    ]:
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module_path)
