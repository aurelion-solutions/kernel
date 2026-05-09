# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for FileLogReader."""

import json
from pathlib import Path
import tempfile

from src.platform.logs.interface import LogReader
from src.platform.logs.providers.file import FileLogReader


def test_file_log_reader_satisfies_protocol() -> None:
    """FileLogReader satisfies LogReader protocol."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        path = Path(f.name)
    try:
        reader: LogReader = FileLogReader(path=path)
        assert hasattr(reader, 'read')
    finally:
        path.unlink(missing_ok=True)


def test_read_missing_file_returns_empty_list() -> None:
    """When file does not exist, read returns empty list."""
    path = Path(tempfile.gettempdir()) / 'nonexistent_aurelion_test_12345.jsonl'
    reader = FileLogReader(path=path)
    assert reader.read(limit=10) == []


def test_read_empty_file_returns_empty_list() -> None:
    """When file is empty, read returns empty list."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        path = Path(f.name)
    try:
        reader = FileLogReader(path=path)
        assert reader.read(limit=10) == []
    finally:
        path.unlink(missing_ok=True)


def test_read_parses_jsonl_records() -> None:
    """read parses JSONL lines into dict objects."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        f.write('{"level":"info","message":"hi"}\n')
        f.write('{"level":"info","message":"bye"}\n')
        path = Path(f.name)
    try:
        reader = FileLogReader(path=path)
        records = reader.read(limit=100)
        assert len(records) == 2
        assert records[0]['message'] == 'hi'
        assert records[1]['message'] == 'bye'
    finally:
        path.unlink(missing_ok=True)


def test_read_respects_limit() -> None:
    """read returns at most limit records (most recent)."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        for i in range(10):
            f.write(json.dumps({'n': i}) + '\n')
        path = Path(f.name)
    try:
        reader = FileLogReader(path=path)
        records = reader.read(limit=3)
        assert len(records) == 3
        assert [r['n'] for r in records] == [7, 8, 9]
    finally:
        path.unlink(missing_ok=True)


def test_read_skips_invalid_lines() -> None:
    """read skips lines that are not valid JSON."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
        f.write('{"valid": true}\n')
        f.write('not json\n')
        f.write('{"also": "valid"}\n')
        path = Path(f.name)
    try:
        reader = FileLogReader(path=path)
        records = reader.read(limit=10)
        assert len(records) == 2
        assert records[0]['valid'] is True
        assert records[1]['also'] == 'valid'
    finally:
        path.unlink(missing_ok=True)
