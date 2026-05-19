# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for SMS channel — file provider + factory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.platform.notifications.sms.factory import (
    SmsSenderFactory,
    UnsupportedSmsProviderError,
)
from src.platform.notifications.sms.interface import SmsMessage
from src.platform.notifications.sms.providers.file import FileSmsSender


@pytest.mark.asyncio
async def test_file_sender_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / 'sms.jsonl'
    sender = FileSmsSender(path=path)

    result = await sender.send(SmsMessage(to='+15555550100', body='Hello', correlation_id='c-1'))
    assert result.sent and result.provider == 'file'

    line = path.read_text(encoding='utf-8').strip()
    record = json.loads(line)
    assert record['channel'] == 'sms'
    assert record['to'] == '+15555550100'
    assert record['body'] == 'Hello'
    assert record['correlation_id'] == 'c-1'


def test_factory_default_is_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AURELION_NOTIFICATIONS_SMS_PROVIDER', raising=False)
    factory = SmsSenderFactory()
    assert factory.default().name == 'file'


def test_factory_unknown_provider_raises() -> None:
    factory = SmsSenderFactory()
    with pytest.raises(UnsupportedSmsProviderError):
        factory.get('no-such-provider')


def test_factory_twilio_registered() -> None:
    factory = SmsSenderFactory()
    assert 'twilio' in factory.list_names()
    assert factory.get('twilio').name == 'twilio'
