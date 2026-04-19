# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AST contract test: EffectiveAccessReadService has no LogService reference, no session mutation, no event emission."""

from __future__ import annotations

import ast
from pathlib import Path


def _get_read_service_class_node() -> ast.ClassDef:
    """Parse service.py and return the EffectiveAccessReadService ClassDef node."""
    service_path = Path(__file__).resolve().parent.parent / 'service.py'
    source = service_path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == 'EffectiveAccessReadService':
            return node
    raise AssertionError(
        f'EffectiveAccessReadService class not found in {service_path}. If the class was renamed, update this test.'
    )


def test_read_service_has_no_logservice_reference() -> None:
    """C1: EffectiveAccessReadService class body must not reference LogService."""
    class_node = _get_read_service_class_node()
    violations = [
        f'line {node.lineno}: ast.Name(id=LogService)'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Name) and node.id == 'LogService'
    ] + [
        f'line {node.lineno}: ast.Attribute(attr=LogService)'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute) and node.attr == 'LogService'
    ]
    assert not violations, f'EffectiveAccessReadService must not reference LogService. Found: {violations}'


def test_read_service_does_not_mutate_session() -> None:
    """C2: EffectiveAccessReadService must not call flush, commit, or rollback."""
    _MUTATION_METHODS = {'flush', 'commit', 'rollback'}
    class_node = _get_read_service_class_node()
    violations = [
        f'line {node.lineno}: .{node.attr}'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute) and node.attr in _MUTATION_METHODS
    ]
    assert not violations, (
        f'EffectiveAccessReadService must not mutate the session (flush/commit/rollback). Found: {violations}'
    )


def test_read_service_does_not_emit_events() -> None:
    """C3: EffectiveAccessReadService must not emit events."""
    _EMIT_METHODS = {'emit_safe', 'emit_log', 'emit'}
    class_node = _get_read_service_class_node()
    violations = [
        f'line {node.lineno}: .{node.attr}'
        for node in ast.walk(class_node)
        if isinstance(node, ast.Attribute) and node.attr in _EMIT_METHODS
    ]
    assert not violations, (
        f'EffectiveAccessReadService must not emit events (emit_safe/emit_log/emit). Found: {violations}'
    )
