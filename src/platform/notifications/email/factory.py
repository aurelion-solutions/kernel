# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmailSender factory — provider resolution by name."""

from __future__ import annotations

from collections.abc import Callable
import os

from src.platform.notifications.email.interface import EmailSender
from src.platform.notifications.email.providers.file import FileEmailSender
from src.platform.notifications.email.providers.smtp import SmtpEmailSender


class UnsupportedEmailProviderError(Exception):
    """Raised when the configured email provider is not registered."""


class EmailSenderFactory:
    """Resolves an ``EmailSender`` by provider name.

    Default selection comes from
    ``AURELION_NOTIFICATIONS_EMAIL_PROVIDER`` (defaults to ``file``).
    """

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], EmailSender]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('file', lambda: FileEmailSender())
        self.register('smtp', lambda: SmtpEmailSender())

    def register(self, name: str, provider_factory: Callable[[], EmailSender]) -> None:
        self._providers[name] = provider_factory

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def list_names(self) -> list[str]:
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> EmailSender:
        if provider_name not in self._providers:
            raise UnsupportedEmailProviderError(f'Unsupported email provider: {provider_name!r}')
        return self._providers[provider_name]()

    def default(self) -> EmailSender:
        """Return the env-selected default ``EmailSender`` (``file`` in dev)."""
        name = os.environ.get('AURELION_NOTIFICATIONS_EMAIL_PROVIDER', 'file')
        return self.get(name)


email_sender_factory = EmailSenderFactory()
