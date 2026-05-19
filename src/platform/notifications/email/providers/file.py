# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-backed email provider for development and tests.

Each ``send`` call appends a JSON line to ``AURELION_NOTIFICATIONS_EMAIL_FILE_PATH``
(default ``./.notifications/email.jsonl``). The provider never raises for
delivery failures — it always returns ``sent=True``. Writes are append-only
and durable across process restarts so tests can scan the file.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Lock

from src.platform.notifications.email.interface import EmailMessage, EmailSendResult

_DEFAULT_PATH = Path('.notifications') / 'email.jsonl'


def _resolve_path() -> Path:
    raw = os.environ.get('AURELION_NOTIFICATIONS_EMAIL_FILE_PATH', '')
    return Path(raw) if raw else Path.cwd() / _DEFAULT_PATH


class FileEmailSender:
    """Append-only JSON-lines email sender. Default in development."""

    name = 'file'

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _resolve_path()
        self._lock = Lock()

    async def send(self, message: EmailMessage) -> EmailSendResult:
        record = {
            'at': datetime.now(UTC).isoformat(),
            'channel': 'email',
            'to': list(message.to),
            'subject': message.subject,
            'body': message.body,
            'locale': message.locale,
            'correlation_id': message.correlation_id,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True)

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')

        return EmailSendResult(
            sent=True,
            provider=self.name,
            provider_message_id=None,
        )
