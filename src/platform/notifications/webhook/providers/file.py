# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-backed webhook provider for development and tests."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Lock

from src.platform.notifications.webhook.interface import WebhookMessage, WebhookSendResult

_DEFAULT_PATH = Path('.notifications') / 'webhook.jsonl'


def _resolve_path() -> Path:
    raw = os.environ.get('AURELION_NOTIFICATIONS_WEBHOOK_FILE_PATH', '')
    return Path(raw) if raw else Path.cwd() / _DEFAULT_PATH


class FileWebhookSender:
    name = 'file'

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _resolve_path()
        self._lock = Lock()

    async def send(self, message: WebhookMessage) -> WebhookSendResult:
        record = {
            'at': datetime.now(UTC).isoformat(),
            'channel': 'webhook',
            'url': message.url,
            'payload': dict(message.payload),
            'headers': dict(message.headers),
            'correlation_id': message.correlation_id,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')

        return WebhookSendResult(sent=True, provider=self.name, status_code=None)
