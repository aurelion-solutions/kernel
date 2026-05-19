# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""InAppSender factory — provider resolution by name."""

from __future__ import annotations

from collections.abc import Callable
import os

from src.platform.notifications.inapp.interface import InAppSender
from src.platform.notifications.inapp.providers.eventbus import EventBusInAppSender
from src.platform.notifications.inapp.providers.file import FileInAppSender


class UnsupportedInAppProviderError(Exception):
    """Raised when the configured inapp provider is not registered."""


class InAppSenderFactory:
    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], InAppSender]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('file', lambda: FileInAppSender())
        self.register('eventbus', lambda: EventBusInAppSender())

    def register(self, name: str, provider_factory: Callable[[], InAppSender]) -> None:
        self._providers[name] = provider_factory

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def list_names(self) -> list[str]:
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> InAppSender:
        if provider_name not in self._providers:
            raise UnsupportedInAppProviderError(f'Unsupported inapp provider: {provider_name!r}')
        return self._providers[provider_name]()

    def default(self) -> InAppSender:
        name = os.environ.get('AURELION_NOTIFICATIONS_INAPP_PROVIDER', 'file')
        return self.get(name)


inapp_sender_factory = InAppSenderFactory()
