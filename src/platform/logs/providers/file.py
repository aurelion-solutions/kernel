# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-based log sink and reader for development."""

import json
import os
from pathlib import Path
from threading import Lock

from src.platform.logs.interface import LogReader, LogSink
from src.platform.logs.schemas import LogEvent

_DEFAULT_PATH = Path('.logs') / 'aurelion.log.jsonl'


def _resolve_path() -> Path:
    raw = os.environ.get('AURELION_LOG_FILE_PATH', '')
    if raw:
        return Path(raw)
    return Path.cwd() / _DEFAULT_PATH


class FileLogSink(LogSink):
    """Append-only JSONL log sink. Writes one JSON object per line."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _resolve_path()
        self._lock = Lock()

    def emit(self, event: LogEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = event.model_dump(mode='json')
        line = json.dumps(payload, ensure_ascii=False) + '\n'
        with self._lock:
            with open(self._path, 'a', encoding='utf-8') as f:
                f.write(line)


class FileLogReader(LogReader):
    """Read recent log records from JSONL file. Same path as FileLogSink."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _resolve_path()

    def read(self, limit: int = 100) -> list[dict]:
        """Read up to limit most recent lines as parsed JSON objects."""
        if not self._path.exists():
            return []
        records: list[dict] = []
        try:
            with open(self._path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return records[-limit:] if len(records) > limit else records
