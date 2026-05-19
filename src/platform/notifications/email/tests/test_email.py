# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for email channel — file provider + factory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.platform.notifications.email.factory import (
    EmailSenderFactory,
    UnsupportedEmailProviderError,
)
from src.platform.notifications.email.interface import EmailMessage
from src.platform.notifications.email.providers.file import FileEmailSender


@pytest.mark.asyncio
async def test_file_sender_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / 'email.jsonl'
    sender = FileEmailSender(path=path)

    result_a = await sender.send(EmailMessage(to=('a@example.com',), subject='Hi', body='Body A', correlation_id='c-1'))
    result_b = await sender.send(EmailMessage(to=('b@example.com', 'c@example.com'), subject='Hey', body='Body B'))

    assert result_a.sent and result_a.provider == 'file' and result_a.provider_message_id is None
    assert result_b.sent

    lines = path.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 2
    rec_a = json.loads(lines[0])
    rec_b = json.loads(lines[1])
    assert rec_a['channel'] == 'email'
    assert rec_a['to'] == ['a@example.com']
    assert rec_a['correlation_id'] == 'c-1'
    assert rec_b['to'] == ['b@example.com', 'c@example.com']


@pytest.mark.asyncio
async def test_file_sender_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / 'nested' / 'deeper' / 'email.jsonl'
    sender = FileEmailSender(path=path)

    await sender.send(EmailMessage(to=('a@example.com',), subject='S', body='B'))
    assert path.exists()


def test_factory_default_is_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AURELION_NOTIFICATIONS_EMAIL_PROVIDER', raising=False)
    factory = EmailSenderFactory()
    sender = factory.default()
    assert sender.name == 'file'


def test_factory_env_selects_smtp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('AURELION_NOTIFICATIONS_EMAIL_PROVIDER', 'smtp')
    factory = EmailSenderFactory()
    sender = factory.default()
    assert sender.name == 'smtp'


def test_factory_unknown_provider_raises() -> None:
    factory = EmailSenderFactory()
    with pytest.raises(UnsupportedEmailProviderError):
        factory.get('no-such-provider')


def test_factory_register_custom_provider() -> None:
    factory = EmailSenderFactory()

    class _CustomSender:
        name = 'custom'

        async def send(self, message: EmailMessage):  # type: ignore[no-untyped-def]
            from src.platform.notifications.email.interface import EmailSendResult

            return EmailSendResult(sent=True, provider=self.name, provider_message_id='x')

    factory.register('custom', lambda: _CustomSender())
    assert 'custom' in factory.list_names()
    assert factory.get('custom').name == 'custom'
