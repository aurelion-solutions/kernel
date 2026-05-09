# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Layer invariant guard — engines must not depend on products.

Enforces the architectural rule:
  "No engine may depend on a product. Direction is always product -> engine -> platform."

AST-scans every ``src/engines/**/*.py`` (excluding ``__pycache__`` and ``tests/``)
for ``from src.products`` / ``import src.products`` and fails on any hit.
The guard fires the day a ``src/products/`` directory is introduced and an engine
reaches into it.
"""

from __future__ import annotations

from pathlib import Path
import re

# Split the forbidden module name at runtime so this file itself does not match
# its own scanner (or any future broader scanner that greps src/ for the string).
_PRODUCTS = 'src.' + 'products'

# Pattern matches both import shapes:
#   from src.products...
#   from src.products import X
#   import src.products
#   import src.products.foo
_IMPORT_PATTERN = re.compile(
    r'^\s*from\s+' + re.escape(_PRODUCTS) + r'(\.|\s|$)'
    r'|'
    r'^\s*import\s+' + re.escape(_PRODUCTS) + r'(\.|\s|$)'
)


def test_engines_do_not_import_from_products() -> None:
    """engines/* must not import from src.products (products are Layer 3, engines are Layer 2)."""
    engines_dir = Path(__file__).parent.parent  # src/engines/

    violations: list[tuple[str, int, str]] = []

    for py_file in engines_dir.rglob('*.py'):
        if '__pycache__' in py_file.parts:
            continue
        if 'tests' in py_file.parts:
            continue
        # Belt-and-braces: skip this file itself in case the tests filter is ever relaxed
        if py_file == Path(__file__):
            continue

        text = py_file.read_text(encoding='utf-8')
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if _IMPORT_PATTERN.match(line):
                violations.append((str(py_file.relative_to(engines_dir.parent)), lineno, stripped))

    if violations:
        lines_str = '\n'.join(f'  {path}:{lineno}: {line}' for path, lineno, line in violations)
        raise AssertionError(
            'engines/* must not import from ' + _PRODUCTS + f'.\nFound {len(violations)} violation(s):\n{lines_str}'
        )
