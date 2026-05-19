# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""File-backed in-app provider for development and tests.

Writes a line per outgoing notification to a JSON-lines file. No MQ event
is emitted — useful for unit tests that want to assert the payload shape
without spinning up RabbitMQ.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from threading import Lock
import uuid

from src.platform.notifications.inapp.interface import InAppMessage, InAppSendResult

_DEFAULT_PATH = Path('.notifications') / 'inapp.jsonl'


def _resolve_path() -> Path:
    raw = os.environ.get('AURELION_NOTIFICATIONS_INAPP_FILE_PATH', '')
    return Path(raw) if raw else Path.cwd() / _DEFAULT_PATH


class FileInAppSender:
    name = 'file'

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _resolve_path()
        self._lock = Lock()

    async def send(self, message: InAppMessage) -> InAppSendResult:
        notification_id = str(uuid.uuid4())
        record = {
            'at': datetime.now(UTC).isoformat(),
            'channel': 'inapp',
            'notification_id': notification_id,
            'template': message.template,
            'recipient_kind': message.recipient_kind,
            'recipient_id': message.recipient_id,
            'routing_key': message.routing_key,
            'subject': message.subject,
            'body': message.body,
            'link_to': message.link_to,
            'case_id': message.case_id,
            'ctx': dict(message.ctx),
            'correlation_id': message.correlation_id,
        }
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)

        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')

        return InAppSendResult(sent=True, provider=self.name, notification_id=notification_id)
