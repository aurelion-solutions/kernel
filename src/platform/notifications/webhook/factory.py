# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""WebhookSender factory — provider resolution by name."""

from __future__ import annotations

from collections.abc import Callable
import os

from src.platform.notifications.webhook.interface import WebhookSender
from src.platform.notifications.webhook.providers.file import FileWebhookSender
from src.platform.notifications.webhook.providers.http import HttpWebhookSender


class UnsupportedWebhookProviderError(Exception):
    """Raised when the configured webhook provider is not registered."""


class WebhookSenderFactory:
    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], WebhookSender]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('file', lambda: FileWebhookSender())
        self.register('http', lambda: HttpWebhookSender())

    def register(self, name: str, provider_factory: Callable[[], WebhookSender]) -> None:
        self._providers[name] = provider_factory

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def list_names(self) -> list[str]:
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> WebhookSender:
        if provider_name not in self._providers:
            raise UnsupportedWebhookProviderError(f'Unsupported webhook provider: {provider_name!r}')
        return self._providers[provider_name]()

    def default(self) -> WebhookSender:
        name = os.environ.get('AURELION_NOTIFICATIONS_WEBHOOK_PROVIDER', 'file')
        return self.get(name)


webhook_sender_factory = WebhookSenderFactory()
