# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Core secrets interface used by the config loader.

This is a minimal Protocol for reading secrets during bootstrap.
It is intentionally separate from src.platform.secrets to avoid a
core → platform dependency.

src.platform.secrets.FileSecretManager satisfies this protocol — the
FileSecretManager.get_secret method is aliased via SecretManagerAdapter.
"""

from src.core.secrets.interface import ConfigSecretManager

__all__ = ['ConfigSecretManager']
