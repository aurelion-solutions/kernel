# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP webhook provider — POST JSON to a URL via httpx.

A 2xx response → ``sent=True``. Anything else returns ``sent=False``
with the HTTP status code and the first 200 chars of the response body.
The provider sets ``content-type: application/json`` plus
``X-Aurelion-Correlation-Id`` when the message carries one; callers can
override either via ``message.headers``.
"""

from __future__ import annotations

import httpx
from src.platform.notifications.webhook.interface import WebhookMessage, WebhookSendResult


class HttpWebhookSender:
    name = 'http'

    async def send(self, message: WebhookMessage) -> WebhookSendResult:
        headers: dict[str, str] = {
            'content-type': 'application/json',
            'user-agent': 'aurelion-notifications/0.1',
        }
        if message.correlation_id is not None:
            headers['X-Aurelion-Correlation-Id'] = message.correlation_id
        headers.update({k: v for k, v in message.headers.items()})

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(message.url, headers=headers, json=dict(message.payload))

        if response.status_code >= 400:
            body = response.text
            return WebhookSendResult(
                sent=False,
                provider=self.name,
                status_code=response.status_code,
                reason=body[:200],
            )

        return WebhookSendResult(
            sent=True,
            provider=self.name,
            status_code=response.status_code,
        )
