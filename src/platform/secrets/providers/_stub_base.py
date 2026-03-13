# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Base class for stub SecretManager providers."""

from src.platform.secrets.interface import SecretManager


class StubSecretManagerBase(SecretManager):
    """Base for stub providers. All methods raise NotImplementedError."""

    def set_secret(self, key: str, value: str) -> None:
        raise NotImplementedError('Stub provider not implemented')

    def get_secret(self, key: str) -> str:
        raise NotImplementedError('Stub provider not implemented')

    def delete_secret(self, key: str) -> None:
        raise NotImplementedError('Stub provider not implemented')
