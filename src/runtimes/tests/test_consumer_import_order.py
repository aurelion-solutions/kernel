# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Regression test: consumer runtimes must NOT raise at import time.

The riskiest assumption from TASK.md:
  mq_log_buffer_consumer and mq_eas_projection_consumer previously imported
  SessionLocal at module level BEFORE load_dotenv() ran, causing KeyError /
  ValidationError when DATABASE_URL was absent.

With the new lazy get_session_factory(), the DSN is read only when the
function is first called — not at import time.  This test verifies that
importing the consumer module with a fully empty environment does NOT raise.

Each import runs in a subprocess so sys.modules and os.environ are clean.
We pass env={} (empty) plus the minimal PATH so Python can locate itself.
A non-zero returncode or ValidationError / KeyError on stderr is a failure.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest


def _run_import_in_subprocess(module_path: str) -> subprocess.CompletedProcess[str]:
    """Import *module_path* in a child process with an empty environment.

    Only PATH is preserved so the Python interpreter can be found.
    No DATABASE_URL, no AURELION_*, no .env loaded.
    """
    script = f'import {module_path}'
    child_env = {'PATH': os.environ.get('PATH', '')}
    return subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        env=child_env,
    )


def test_mq_log_buffer_consumer_no_import_error() -> None:
    """Importing mq_log_buffer_consumer.main with empty env does not raise."""
    pytest.importorskip('pika', reason='pika not installed — skipping consumer import test')
    result = _run_import_in_subprocess('src.runtimes.mq_log_buffer_consumer.main')
    assert result.returncode == 0, (
        f'mq_log_buffer_consumer raised at import time.\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}'
    )
    assert 'ValidationError' not in result.stderr, f'ValidationError detected at import time:\n{result.stderr}'
    assert 'KeyError' not in result.stderr, f'KeyError detected at import time:\n{result.stderr}'


def test_mq_eas_projection_consumer_no_import_error() -> None:
    """Importing mq_eas_projection_consumer.main with empty env does not raise."""
    pytest.importorskip('pika', reason='pika not installed — skipping consumer import test')
    result = _run_import_in_subprocess('src.runtimes.mq_eas_projection_consumer.main')
    assert result.returncode == 0, (
        f'mq_eas_projection_consumer raised at import time.\nstderr:\n{result.stderr}\nstdout:\n{result.stdout}'
    )
    assert 'ValidationError' not in result.stderr, f'ValidationError detected at import time:\n{result.stderr}'
    assert 'KeyError' not in result.stderr, f'KeyError detected at import time:\n{result.stderr}'


def test_session_module_has_no_module_level_engine() -> None:
    """session.py must not create engine at module import time."""
    # Remove session module from cache
    for key in list(sys.modules.keys()):
        if 'src.core.db.session' in key:
            del sys.modules[key]

    mod = importlib.import_module('src.core.db.session')
    assert not hasattr(mod, 'engine'), "Module-level 'engine' must not exist in session.py"
    assert not hasattr(mod, 'SessionLocal'), "Module-level 'SessionLocal' must not exist in session.py"
