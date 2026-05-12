# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Static vocabulary guard: every action_slug literal in every handler module
must appear in the canonical ref_actions vocabulary.

Uses ast.parse — no imports, no runtime DB queries. Hardcodes the seven
canonical slugs from migration 2026_04_24_0000_add_ref_actions.py.

Walk strategy:
1. Find all .py files under handlers/ (exclude __init__.py, tests/).
2. ast.parse each module.
3. Collect slugs from:
   a. keyword args  `action_slug=<literal>` in ast.Call nodes.
   b. values of module-scope ast.Assign targets whose name matches
      patterns like _PRIVILEGE_TO_ACTION_SLUG (captures the db_grant
      mapping table).
4. Assert collected_slugs ⊆ VALID_ACTION_SLUGS.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re

# Canonical vocabulary — frozen in migration 2026_04_24_0000_add_ref_actions.py.
# If a new slug is needed, update the migration AND this set together.
VALID_ACTION_SLUGS: frozenset[str] = frozenset({'read', 'write', 'execute', 'approve', 'admin', 'use', 'own'})

_DICT_NAME_RE = re.compile(r'.*[Ss]lug.*|.*_TO_ACTION.*|.*_MAP.*', re.IGNORECASE)

_HANDLERS_DIR = Path(__file__).resolve().parent.parent / 'handlers'


def _collect_slugs_from_module(source: str) -> set[str]:
    """Return all action_slug string literals found in the given source code."""
    tree = ast.parse(source)
    slugs: set[str] = set()

    # Walk module-scope Assign nodes for _PRIVILEGE_TO_ACTION_SLUG-like dicts
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if not _DICT_NAME_RE.match(target.id):
                continue
            # Collect values from the dict literal
            if isinstance(node.value, ast.Dict):
                for val in node.value.values:
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        slugs.add(val.value)

    # Walk all Call nodes for action_slug= keyword arguments
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == 'action_slug' and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    slugs.add(kw.value.value)

    return slugs


def _collect_handler_modules() -> list[Path]:
    """Return all .py handler modules excluding __init__.py and tests/."""
    return [p for p in _HANDLERS_DIR.rglob('*.py') if p.name != '__init__.py' and 'tests' not in p.parts]


def test_handler_action_slugs_are_all_seeded():
    """Every action_slug literal in every handler module is in VALID_ACTION_SLUGS."""
    handler_files = _collect_handler_modules()
    assert handler_files, f'No handler modules found under {_HANDLERS_DIR}'

    offenders: dict[str, set[str]] = {}

    for path in handler_files:
        source = path.read_text(encoding='utf-8')
        slugs = _collect_slugs_from_module(source)
        unknown = slugs - VALID_ACTION_SLUGS
        if unknown:
            offenders[str(path.relative_to(_HANDLERS_DIR))] = unknown

    assert not offenders, (
        'Handler vocabulary guard failed — unknown action slugs detected:\n'
        + '\n'.join(f'  {module}: {", ".join(sorted(bad))}' for module, bad in sorted(offenders.items()))
        + f'\nValid slugs: {sorted(VALID_ACTION_SLUGS)}'
    )
