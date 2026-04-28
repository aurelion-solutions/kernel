# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Bootstrap configuration package.

Public surface:
    get_settings() -> Settings   (lru_cache singleton per process)
    Settings, PostgresSettings, RabbitMQSettings, AppSettings, LakeStaticSettings

The old module-level ``settings = Settings()`` singleton is gone.
Call ``get_settings()`` everywhere.

Startup contract:
    1. load_dotenv()  (in each entrypoint, before any src.* import that reads config)
    2. get_settings() — reads AURELION_SECRET_PROVIDER + AURELION_SECRETS_FILE,
       constructs the secret manager, calls load_settings().
"""

from __future__ import annotations

import os
from functools import lru_cache

from src.core.config.loader import load_settings
from src.core.config.settings import (
    AppSettings,
    LakeStaticSettings,
    PostgresSettings,
    RabbitMQSettings,
    Settings,
)
from src.core.secrets.factory import config_secret_manager_factory

__all__ = [
    'AppSettings',
    'LakeStaticSettings',
    'PostgresSettings',
    'RabbitMQSettings',
    'Settings',
    'get_settings',
]


@lru_cache
def get_settings() -> Settings:
    """Return the process-scoped bootstrap configuration.

    Reads ``AURELION_SECRET_PROVIDER`` (default ``file``) from the environment,
    resolves the provider via ``config_secret_manager_factory``, and delegates
    to :func:`src.core.config.loader.load_settings`.

    Providers must be registered before the first call by calling
    ``register_default_providers()`` from ``src.platform.secrets.factory``
    in each entrypoint.

    Results are cached for the lifetime of the process.  Call
    ``get_settings.cache_clear()`` in tests that need a fresh instance.
    """
    provider_name = os.environ.get('AURELION_SECRET_PROVIDER', 'file')
    sm = config_secret_manager_factory.get(provider_name)
    return load_settings(sm)
