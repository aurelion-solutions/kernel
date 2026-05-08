# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AST contract tests: CapabilityResolverService has no LogService reference,
no session mutation, no event emission.

Three discrete tests so a failure message points at the exact violation category.
Mirrors effective_access/tests/test_read_service_contract.py.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _get_resolver_class_node() -> ast.ClassDef:
    """Parse capability_resolver.py and return the CapabilityResolverService ClassDef node."""
    resolver_path = Path(__file__).resolve().parent.parent / 'capability_resolver.py'
    source = resolver_path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'CapabilityResolverService':
            return node
    raise AssertionError(
        f'CapabilityResolverService class not found in {resolver_path}. If the class was renamed, update this test.'
    )


def test_capability_resolver_has_no_logservice_reference() -> None:
    """C1: CapabilityResolverService class body must not reference LogService."""
    class_node = _get_resolver_class_node()
    violations = [
        f'line {node.lineno}: ast.Name(id=LogService)'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Name) and node.id == 'LogService'
    ] + [
        f'line {node.lineno}: ast.Attribute(attr=LogService)'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute) and node.attr == 'LogService'
    ]
    assert not violations, f'CapabilityResolverService must not reference LogService. Found: {violations}'


def test_capability_resolver_does_not_mutate_session() -> None:
    """C2: CapabilityResolverService must not call flush, commit, or rollback."""
    _MUTATION_METHODS = {'flush', 'commit', 'rollback'}
    class_node = _get_resolver_class_node()
    violations = [
        f'line {node.lineno}: .{node.attr}'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute) and node.attr in _MUTATION_METHODS
    ]
    assert not violations, (
        f'CapabilityResolverService must not mutate the session (flush/commit/rollback). Found: {violations}'
    )


def test_capability_resolver_does_not_emit_events() -> None:
    """C3: CapabilityResolverService must not emit events."""
    _EMIT_METHODS = {'emit_safe', 'emit_log', 'emit'}
    class_node = _get_resolver_class_node()
    violations = [
        f'line {node.lineno}: .{node.attr}'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute) and node.attr in _EMIT_METHODS
    ]
    assert not violations, (
        f'CapabilityResolverService must not emit events (emit_safe/emit_log/emit). Found: {violations}'
    )
