# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DataLakeStorage factory for provider resolution by name."""

from collections.abc import Callable

from src.platform.storage.interface import DataLakeStorage
from src.platform.storage.providers.file import FileDataLakeStorage
from src.platform.storage.providers.iceberg import IcebergStubDataLakeStorage
from src.platform.storage.providers.s3 import S3StubDataLakeStorage


class UnsupportedProviderError(Exception):
    """Raised when the requested storage provider is not registered."""


class DataLakeStorageFactory:
    """Resolves DataLakeStorage by provider name. Uses lazy instantiation."""

    def __init__(self) -> None:
        self._providers: dict[str, Callable[[], DataLakeStorage]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        self.register('file', lambda: FileDataLakeStorage())
        self.register('s3', lambda: S3StubDataLakeStorage())
        self.register('iceberg', lambda: IcebergStubDataLakeStorage())

    def register(
        self,
        name: str,
        provider_factory: Callable[[], DataLakeStorage],
    ) -> None:
        """Register a provider factory. Called for each get()."""
        self._providers[name] = provider_factory

    def list_names(self) -> list[str]:
        """Return list of registered provider names."""
        return sorted(self._providers.keys())

    def get(self, provider_name: str) -> DataLakeStorage:
        """Return a new DataLakeStorage instance for the given provider."""
        if provider_name not in self._providers:
            raise UnsupportedProviderError(f'Unsupported storage provider: {provider_name!r}')
        return self._providers[provider_name]()


data_lake_factory = DataLakeStorageFactory()
