# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-backed SMS provider for development and tests.

Append-only JSON lines, identical shape to the email/file provider.
Path: ``AURELION_NOTIFICATIONS_SMS_FILE_PATH`` (default ``./.notifications/sms.jsonl``).
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Lock

from src.platform.notifications.sms.interface import SmsMessage, SmsSendResult

_DEFAULT_PATH = Path('.notifications') / 'sms.jsonl'


def _resolve_path() -> Path:
    raw = os.environ.get('AURELION_NOTIFICATIONS_SMS_FILE_PATH', '')
    return Path(raw) if raw else Path.cwd() / _DEFAULT_PATH


class FileSmsSender:
    name = 'file'

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _resolve_path()
        self._lock = Lock()

    async def send(self, message: SmsMessage) -> SmsSendResult:
        record = {
            'at': datetime.now(UTC).isoformat(),
            'channel': 'sms',
            'to': message.to,
            'body': message.body,
            'locale': message.locale,
            'correlation_id': message.correlation_id,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')

        return SmsSendResult(sent=True, provider=self.name, provider_message_id=None)
