# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform secrets package."""

from src.platform.secrets.factory import SecretManagerFactory, UnsupportedProviderError, secret_manager_factory
from src.platform.secrets.interface import SecretManager
from src.platform.secrets.providers.file import FileSecretManager

__all__ = [
    'FileSecretManager',
    'SecretManager',
    'SecretManagerFactory',
    'UnsupportedProviderError',
    'secret_manager_factory',
]
