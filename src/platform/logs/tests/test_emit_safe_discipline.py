# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""emit_safe naming-discipline meta-test.

Enforces the ARCH_CONTEXT rule (post-Phase 17 Step 17):

  Every emit_safe(...) call in src/engines/**/service.py and
  src/inventory/**/service.py MUST carry an explicit
  # allowed-emit-safe: <reason> marker — either inline on the call line
  or on the line immediately above. Reason vocabulary (fixed):
  observability | provider boundary | best-effort warning.

This test is a textual line-scan, not an AST walk — same shape as
src/engines/reconciliation/tests/test_layer_invariants.py.

The identifier-split trick below prevents this file from matching its own
scanner if a future broader scan ever sweeps src/platform/logs/tests/.
"""

from __future__ import annotations

from pathlib import Path
import re

# Split identifiers at runtime so this file does not match its own scanner.
_EMIT_SAFE = 'emit' + '_safe'
_MARKER = '# allowed-' + 'emit-safe:'

_CALL_PATTERN = re.compile(r'\.' + _EMIT_SAFE + r'\(')


def test_emit_safe_call_sites_carry_allowed_marker() -> None:
    """Every .emit_safe( call in engines/**/service.py and inventory/**/service.py carries a marker.

    Accepted placements (both checked):
    - Inline: the call line itself contains ``# allowed-emit-safe: <reason>``.
    - Line-above: the immediately-preceding non-blank line contains the marker.

    The predecessor walk does NOT skip comment lines — a ``# noqa`` immediately
    above a call is NOT a valid marker placement.
    """
    # Resolve aurelion-kernel/src/ as the root for the two scopes.
    kernel_src = Path(__file__).resolve().parents[3]  # src/

    scopes = [
        kernel_src / 'engines',
        kernel_src / 'inventory',
    ]

    violations: list[tuple[str, int, str]] = []

    for scope_root in scopes:
        for py_file in sorted(scope_root.rglob('*.py')):
            # Skip __pycache__ directories.
            if '__pycache__' in py_file.parts:
                continue
            # Skip test directories — rule scope is production service.py only.
            if 'tests' in py_file.parts:
                continue
            # Rule scope is service.py ONLY.
            if py_file.name != 'service.py':
                continue

            text = py_file.read_text(encoding='utf-8')
            lines = text.splitlines()

            rel_path = str(py_file.relative_to(kernel_src.parent))

            for lineno, line in enumerate(lines, start=1):
                stripped = line.strip()
                # Skip blank lines, comment-only lines, and docstring boundary lines.
                if not stripped:
                    continue
                if stripped.startswith('#'):
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    continue

                # Check whether this line contains a .emit_safe( call.
                if not _CALL_PATTERN.search(line):
                    continue

                # Check inline placement first.
                if _MARKER in line:
                    continue

                # Check line-above placement: walk back from the previous line,
                # skipping blank lines only (NOT comment lines — per §9.bis B).
                above_ok = False
                check_idx = lineno - 2  # 0-indexed; lineno is 1-indexed
                while check_idx >= 0:
                    prev = lines[check_idx]
                    prev_stripped = prev.strip()
                    if prev_stripped == '':
                        check_idx -= 1
                        continue
                    # First non-blank predecessor — must contain the marker.
                    if _MARKER in prev:
                        above_ok = True
                    break

                if not above_ok:
                    violations.append((rel_path, lineno, stripped))

    if violations:
        lines_str = '\n'.join(f'  {path}:{lineno}: {line}' for path, lineno, line in violations)
        raise AssertionError(
            'emit_safe discipline violated: every .emit_safe( call in\n'
            '  src/engines/**/service.py and src/inventory/**/service.py\n'
            'must carry an explicit # allowed-emit-safe: <reason> marker\n'
            '(inline on the call line or on the line immediately above).\n\n'
            f'Found {len(violations)} unmarked call site(s):\n{lines_str}\n\n'
            'Reason vocabulary: observability | provider boundary | best-effort warning\n'
            'See ARCH_CONTEXT.md (post-Phase 17 Step 17) and\n'
            '    src/platform/logs/tests/test_emit_safe_discipline.py'
        )
