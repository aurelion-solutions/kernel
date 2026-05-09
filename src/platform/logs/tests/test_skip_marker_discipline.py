# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Skip-marker discipline meta-test.

Enforces the Phase 17 Step 24 rule:

  Every ``pytest.mark.skip(reason=...)`` and every
  ``pytestmark = pytest.mark.skip(reason=...)`` in
  ``aurelion-kernel/src/**/*.py`` MUST contain one of:
    - ``Phase NN``  (e.g. ``Phase 17``, ``Phase 18+``)
    - ``Step NN``   (e.g. ``Step 12``)
    - ``housekeeping-backlog``

  in the reason string.

``pytest.mark.skipif(...)`` is exempt — environmental / OS / dependency-gate
skips are operational and need no phase reference (§2.1(b) of TASK.md).

This test is a textual line-scan, not an AST walk — same shape as
``src/platform/logs/tests/test_emit_safe_discipline.py`` (Step 17) and
``src/platform/logs/tests/test_broad_except_discipline.py`` (Step 21).

The identifier-split trick below prevents this file from matching its own
scanner if a future broader scan ever sweeps ``src/platform/logs/tests/``.
"""

from __future__ import annotations

from pathlib import Path
import re

# Split identifiers at runtime so this file does not match its own scanner.
_SKIP_MARK = 'pytest.mark.' + 'skip'
# Also split 'nope' so the negative-path synthetic string below does not
# accidentally trigger the scanner when this file is included in a sweep.
_NAKED_REASON = 'no' + 'pe'
_REASON_PATTERN = re.compile(r'pytest\.mark\.skip\s*\(\s*reason\s*=\s*([\'"])(.*?)\1')

# Valid phase-reference tokens (case-sensitive).
_VALID_TOKENS = ('Phase ', 'Step ', 'housekeeping-backlog')


def _extract_skip_reasons(source: str) -> list[tuple[int, str]]:
    """Return [(line_no, reason_text), ...] for every pytest.mark.skip(reason=...) found.

    Excludes ``pytest.mark.skipif`` — environmental gates are exempt.
    """
    results: list[tuple[int, str]] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        # Only lines that contain pytest.mark.skip( but NOT pytest.mark.skipif(
        if _SKIP_MARK + '(' not in line:
            continue
        if _SKIP_MARK + 'if(' in line or _SKIP_MARK + 'if (' in line:
            continue
        m = _REASON_PATTERN.search(line)
        if m:
            results.append((lineno, m.group(2)))
    return results


def test_every_skip_marker_in_kernel_src_carries_a_phase_reference() -> None:
    """Every pytest.mark.skip(reason=...) in aurelion-kernel/src/**/*.py has a phase reference.

    Accepted reason tokens (any one sufficient):
    - ``Phase NN``          e.g. ``Phase 17``, ``Phase 18+``
    - ``Step NN``           e.g. ``Step 12``
    - ``housekeeping-backlog``

    ``pytest.mark.skipif`` sites are exempt (operational / env-gated).
    """
    kernel_src = Path(__file__).resolve().parents[3]  # src/

    violations: list[tuple[str, int, str]] = []

    # Exclude this meta-test itself — its f-string synthetic contains the pattern.
    this_file = Path(__file__).resolve()

    for py_file in sorted(kernel_src.rglob('*.py')):
        # Skip __pycache__ and .venv directories.
        if '__pycache__' in py_file.parts:
            continue
        if '.venv' in py_file.parts:
            continue
        # Skip this meta-test file itself.
        if py_file == this_file:
            continue

        text = py_file.read_text(encoding='utf-8')
        rel_path = str(py_file.relative_to(kernel_src.parent))

        for lineno, reason in _extract_skip_reasons(text):
            if not any(token in reason for token in _VALID_TOKENS):
                violations.append((rel_path, lineno, reason))

    if violations:
        lines_str = '\n'.join(f'  {path}:{lineno}: reason={reason!r}' for path, lineno, reason in violations)
        raise AssertionError(
            'Skip-marker discipline violated: every pytest.mark.skip(reason=...) in\n'
            '  aurelion-kernel/src/**/*.py\n'
            'must contain one of: "Phase NN", "Step NN", or "housekeeping-backlog".\n\n'
            f'Found {len(violations)} offending marker(s):\n{lines_str}\n\n'
            'Fix: update the reason string to reference the deferring phase or step,\n'
            'or use "housekeeping-backlog" for non-phase-gated skips.\n'
            'pytest.mark.skipif is exempt (environmental gates need no phase reference).\n\n'
            'See Phase 17 Step 24 TASK.md §2.1(b).'
        )


def test_meta_test_helper_rejects_naked_skip() -> None:
    """Negative path: helper detects a reason string with no phase reference."""
    # Use _NAKED_REASON so this string is not matched by the scanner above.
    synthetic = f"pytestmark = pytest.mark.skip(reason='{_NAKED_REASON}')\n"
    hits = _extract_skip_reasons(synthetic)
    assert hits, 'helper should have detected the naked skip reason'
    _lineno, reason = hits[0]
    assert not any(token in reason for token in _VALID_TOKENS), f'Expected no valid token in {reason!r} but found one'
