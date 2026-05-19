# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SmsSender factory — provider resolution by name."""

from __future__ import annotations

from collections.abc import Callable
import os

from src.platform.notifications.sms.interface import SmsSender
from src.platform.notifications.sms.providers.file import FileSmsSender
from src.platform.notifications.sms.providers.twilio import TwilioSmsSender


class UnsupportedSmsProviderError(Exception):
    """Raised when the configured SMS provider is not registered."""


class SmsSenderFactory:
    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], SmsSender]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('file', lambda: FileSmsSender())
        self.register('twilio', lambda: TwilioSmsSender())

    def register(self, name: str, provider_factory: Callable[[], SmsSender]) -> None:
        self._providers[name] = provider_factory

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def list_names(self) -> list[str]:
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> SmsSender:
        if provider_name not in self._providers:
            raise UnsupportedSmsProviderError(f'Unsupported SMS provider: {provider_name!r}')
        return self._providers[provider_name]()

    def default(self) -> SmsSender:
        name = os.environ.get('AURELION_NOTIFICATIONS_SMS_PROVIDER', 'file')
        return self.get(name)


sms_sender_factory = SmsSenderFactory()
