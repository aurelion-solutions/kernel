# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Core-layer registry for ConfigSecretManager providers.

Lives in core so get_settings() can use it without a core→platform dependency.
Platform providers register themselves by calling register_default_providers()
from src.platform.secrets.factory before any entrypoint calls get_settings().
"""

from collections.abc import Callable

from src.core.secrets.interface import ConfigSecretManager


class UnsupportedProviderError(Exception):
    """Raised when no provider is registered for the requested name."""


class ConfigSecretManagerFactory:
    """Registry for ConfigSecretManager providers.

    Pure registry — contains no provider implementations.
    Implementations live in src.platform.secrets.providers and are
    registered at entrypoint startup via register_default_providers().
    """

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], ConfigSecretManager]] = {}

    def register(self, name: str, factory: Callable[[], ConfigSecretManager]) -> None:
        """Register a provider factory callable under *name*."""
        self._providers[name] = factory

    def get(self, provider_name: str) -> ConfigSecretManager:
        """Return a new instance for *provider_name*.

        Raises UnsupportedProviderError when the name is not registered.
        Hint: call register_default_providers() from src.platform.secrets.factory
        before the first get_settings() call.
        """
        if provider_name not in self._providers:
            raise UnsupportedProviderError(
                f'Secret provider {provider_name!r} is not registered. '
                'Call register_default_providers() from src.platform.secrets.factory '
                'before get_settings().'
            )
        return self._providers[provider_name]()

    def list_names(self) -> list[str]:
        return sorted(self._providers)


config_secret_manager_factory = ConfigSecretManagerFactory()
