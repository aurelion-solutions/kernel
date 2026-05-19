# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for webhook channel — file provider + factory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.platform.notifications.webhook.factory import (
    UnsupportedWebhookProviderError,
    WebhookSenderFactory,
)
from src.platform.notifications.webhook.interface import WebhookMessage
from src.platform.notifications.webhook.providers.file import FileWebhookSender


@pytest.mark.asyncio
async def test_file_sender_appends_jsonl(tmp_path: Path) -> None:
    path = tmp_path / 'webhook.jsonl'
    sender = FileWebhookSender(path=path)

    result = await sender.send(
        WebhookMessage(
            url='https://example.com/hooks/x',
            payload={'event': 'case.created', 'id': 'abc'},
            headers={'X-Tenant': 't1'},
            correlation_id='c-1',
        )
    )
    assert result.sent and result.provider == 'file'

    record = json.loads(path.read_text(encoding='utf-8').strip())
    assert record['channel'] == 'webhook'
    assert record['url'] == 'https://example.com/hooks/x'
    assert record['payload'] == {'event': 'case.created', 'id': 'abc'}
    assert record['headers'] == {'X-Tenant': 't1'}
    assert record['correlation_id'] == 'c-1'


def test_factory_default_is_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AURELION_NOTIFICATIONS_WEBHOOK_PROVIDER', raising=False)
    factory = WebhookSenderFactory()
    assert factory.default().name == 'file'


def test_factory_http_registered() -> None:
    factory = WebhookSenderFactory()
    assert 'http' in factory.list_names()
    assert factory.get('http').name == 'http'


def test_factory_unknown_provider_raises() -> None:
    factory = WebhookSenderFactory()
    with pytest.raises(UnsupportedWebhookProviderError):
        factory.get('no-such-provider')
