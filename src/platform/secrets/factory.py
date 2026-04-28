# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SecretManager factory for provider resolution by name."""

from collections.abc import Callable

from src.core.secrets.factory import config_secret_manager_factory
from src.platform.secrets.interface import SecretManager
from src.platform.secrets.providers.akeyless import AkeylessSecretManager
from src.platform.secrets.providers.conjur import ConjurSecretManager
from src.platform.secrets.providers.file import FileSecretManager
from src.platform.secrets.providers.openbao import OpenBaoSecretManager
from src.platform.secrets.providers.vault import VaultSecretManager


class UnsupportedProviderError(Exception):
    """Raised when the requested secret provider is not registered."""


class SecretManagerFactory:
    """Resolves SecretManager by provider name. Uses lazy instantiation."""

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], SecretManager]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('file', lambda: FileSecretManager())
        self.register('vault', lambda: VaultSecretManager())
        self.register('akeyless', lambda: AkeylessSecretManager())
        self.register('conjur', lambda: ConjurSecretManager())
        self.register('openbao', lambda: OpenBaoSecretManager())

    def register(self, name: str, provider_factory: Callable[[], SecretManager]) -> None:
        """Register a provider factory. Called for each get()."""
        self._providers[name] = provider_factory

    def unregister(self, name: str) -> None:
        """Unregister a provider. Only custom providers can be unregistered."""
        self._providers.pop(name, None)

    def list_names(self) -> list[str]:
        """Return list of registered provider names."""
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> SecretManager:
        """Return a new SecretManager instance for the given provider."""
        if provider_name not in self._providers:
            raise UnsupportedProviderError(f'Unsupported secret provider: {provider_name!r}')
        return self._providers[provider_name]()


secret_manager_factory = SecretManagerFactory()


def register_default_providers() -> None:
    """Register all platform secret providers into the core config factory.

    Must be called in every entrypoint before the first get_settings() call.
    Safe to call multiple times — register() is idempotent (overwrites same key).
    """
    config_secret_manager_factory.register('file', FileSecretManager)
    config_secret_manager_factory.register('vault', VaultSecretManager)
    config_secret_manager_factory.register('akeyless', AkeylessSecretManager)
    config_secret_manager_factory.register('conjur', ConjurSecretManager)
    config_secret_manager_factory.register('openbao', OpenBaoSecretManager)
