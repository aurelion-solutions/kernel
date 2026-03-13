# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Provider service for factory registration."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.secrets.factory import SecretManagerFactory, secret_manager_factory
from src.platform.secrets.provider_config.models import Provider
from src.platform.secrets.provider_config.repository import create_provider, delete_provider
from src.platform.secrets.providers.file import FileSecretManager

BUILTIN_PROVIDERS = frozenset(
    {
        'file',
        'vault',
        'akeyless',
        'conjur',
        'openbao',
    }
)


def _register_provider_in_factory(factory: SecretManagerFactory, name: str, type: str, config: dict) -> None:
    """Register a provider in the factory based on type and config."""
    if type == 'file':
        path = config.get('path', '.secrets.json')
        factory.register(name, lambda p=path: FileSecretManager(path=Path(p)))
    else:
        raise ValueError(f'Unsupported provider type: {type!r}')


async def create_provider_and_register(
    session: AsyncSession,
    name: str,
    type: str,
    config: dict,
    factory: SecretManagerFactory | None = None,
) -> Provider:
    """Create provider in DB and register in factory."""
    if name in BUILTIN_PROVIDERS:
        raise ValueError(f'Provider name {name!r} conflicts with built-in provider')
    factory = factory or secret_manager_factory
    provider = await create_provider(session, name=name, type=type, config=config)
    _register_provider_in_factory(factory, name, type, config)
    return provider


async def delete_provider_and_unregister(
    session: AsyncSession,
    name: str,
    factory: SecretManagerFactory | None = None,
) -> bool:
    """Delete provider from DB and unregister from factory."""
    if name in BUILTIN_PROVIDERS:
        return False
    fac = factory or secret_manager_factory
    deleted = await delete_provider(session, name)
    if deleted:
        fac.unregister(name)
    return deleted
