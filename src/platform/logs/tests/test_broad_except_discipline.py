# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Broad-except discipline meta-test.

Enforces the ARCH_CONTEXT rule (post-Phase 17 Step 21):

  Every ``# noqa: BLE001`` token in ``src/`` MUST be accompanied by an
  inline ``# allowed-broad: <reason>`` companion comment on the same line.
  The ``<reason>`` must be one of the eight fixed vocabulary tokens.

This test is a textual line-scan, not an AST walk — same shape as
``src/platform/logs/tests/test_emit_safe_discipline.py``.

The identifier-split trick below prevents this file from matching its own
scanner when it sweeps ``src/``.
"""

from __future__ import annotations

from pathlib import Path
import re

# Split identifiers at runtime so this file does not match its own scanner.
_NOQA = '# ' + 'noqa: BLE001'
_MARKER_PREFIX = '# allowed-' + 'broad:'

# Regex to extract the reason token after ``# allowed-broad:``.
# Matches any word/hyphen sequence up to end-of-line (strips trailing whitespace).
_REASON_RE = re.compile(r'# allowed-broad:\s+([\w\s\-]+?)\s*$')

# Fixed 8-token vocabulary (case-sensitive, exact match).
_ALLOWED_REASONS: frozenset[str] = frozenset(
    [
        'provider boundary',
        'best-effort cleanup',
        'task-loop guard',
        'pipeline boundary',
        'event handler swallow',
        'best-effort log',
        'test fixture cleanup',
        'test orchestration',
    ]
)


def test_broad_except_noqa_sites_carry_allowed_marker() -> None:
    """Every ``# noqa: BLE001`` token in src/ has an inline ``# allowed-broad: <reason>`` companion.

    Rules checked (both required for each matching line):
    - The same line that carries ``# noqa: BLE001`` also carries
      ``# allowed-broad: <reason>`` (inline-only; no line-above placement
      because ``# noqa`` must reside on the offending line per ruff semantics).
    - The ``<reason>`` after ``# allowed-broad:`` is one of the eight fixed
      vocabulary tokens listed in ARCH_CONTEXT.md §broad-except.

    Reports file:line:text for each violation.
    """
    kernel_src = Path(__file__).resolve().parents[3]  # src/

    violations: list[tuple[str, int, str, str]] = []  # (rel_path, lineno, line_text, problem)

    # Resolve this file's path so we can exclude it from the scan.
    this_file = Path(__file__).resolve()

    for py_file in sorted(kernel_src.rglob('*.py')):
        # Skip __pycache__ directories.
        if '__pycache__' in py_file.parts:
            continue
        # Skip this meta-test file itself (its docstrings reference the noqa token).
        if py_file == this_file:
            continue

        text = py_file.read_text(encoding='utf-8')
        lines = text.splitlines()
        rel_path = str(py_file.relative_to(kernel_src.parent))

        for lineno, line in enumerate(lines, start=1):
            # Only process lines that carry the noqa token.
            if _NOQA not in line:
                continue

            # Check that the companion marker is present on the same line.
            if _MARKER_PREFIX not in line:
                violations.append((rel_path, lineno, line.strip(), 'missing # allowed-broad: <reason> companion'))
                continue

            # Extract and validate the reason token.
            m = _REASON_RE.search(line)
            if m is None:
                violations.append((rel_path, lineno, line.strip(), 'could not extract reason after # allowed-broad:'))
                continue

            reason = m.group(1).strip()
            if reason not in _ALLOWED_REASONS:
                violations.append(
                    (
                        rel_path,
                        lineno,
                        line.strip(),
                        f'unknown reason {reason!r} — must be one of the 8 fixed vocabulary tokens',
                    )
                )

    if violations:
        lines_str = '\n'.join(f'  {path}:{lineno}: {problem}\n    {text}' for path, lineno, text, problem in violations)
        vocab_str = '\n'.join(f'    {r}' for r in sorted(_ALLOWED_REASONS))
        raise AssertionError(
            'Broad-except discipline violated: every # noqa: BLE001 in src/\n'
            'must carry an inline # allowed-broad: <reason> companion comment,\n'
            'and <reason> must be one of the 8 fixed vocabulary tokens.\n\n'
            f'Found {len(violations)} violation(s):\n{lines_str}\n\n'
            f'Fixed vocabulary (8 tokens):\n{vocab_str}\n\n'
            'See ARCH_CONTEXT.md (post-Phase 17 Step 21) and\n'
            '    src/platform/logs/tests/test_broad_except_discipline.py'
        )
