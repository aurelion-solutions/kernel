# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Invariant: no AccessFactService / ArtifactBindingService calls in reconciliation slice.

AST-grep / import scan confirming that Step 8 invariant survives Step 9.
Uses AST-level checks rather than naive text search to avoid false positives
from docstrings mentioning the removed dependencies.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SLICE_ROOT = Path(__file__).parent.parent

# Names that must not appear in any *import* statement in non-test source files.
_FORBIDDEN_IMPORTS = {
    'AccessFactService',
    'ArtifactBindingService',
}

# Method names that must not be called anywhere in non-test source files.
_FORBIDDEN_CALLS = {
    'create_fact',
    'revoke_fact',
    'refresh_fact_fields',
}


def _collect_source_files() -> list[Path]:
    """Return all .py files in the reconciliation slice, excluding tests/."""
    return [p for p in _SLICE_ROOT.rglob('*.py') if 'tests' not in p.parts]


def test_no_access_fact_service_in_slice():
    """No reconciliation source file imports AccessFactService or ArtifactBindingService."""
    violations: list[str] = []

    for path in _collect_source_files():
        source = path.read_text()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_IMPORTS or (alias.asname and alias.asname in _FORBIDDEN_IMPORTS):
                        violations.append(f'{path.name}: import {alias.name}')
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in _FORBIDDEN_IMPORTS:
                        violations.append(f'{path.name}: from {node.module} import {alias.name}')

    assert not violations, (
        'Phase 15 Step 8/9 invariant violated — forbidden imports found in reconciliation slice:\n'
        + '\n'.join(violations)
    )


def test_no_access_fact_calls_via_ast():
    """No reconciliation source file calls create_fact, revoke_fact, or refresh_fact_fields."""
    violations: list[str] = []

    for path in _collect_source_files():
        source = path.read_text()
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            # Method call: something.create_fact(...)
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in _FORBIDDEN_CALLS:
                    violations.append(f'{path.name}: call to .{func.attr}()')
                # Direct call: create_fact(...)
                elif isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
                    violations.append(f'{path.name}: call to {func.id}()')

    assert not violations, 'Forbidden method calls found in reconciliation slice:\n' + '\n'.join(violations)
