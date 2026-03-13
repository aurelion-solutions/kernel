# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform storage package — Data Lake abstraction."""

from src.platform.storage.factory import (
    DataLakeStorageFactory,
    UnsupportedProviderError,
    data_lake_factory,
)
from src.platform.storage.interface import DataLakeStorage
from src.platform.storage.providers.file import FileDataLakeStorage

__all__ = [
    'DataLakeStorage',
    'DataLakeStorageFactory',
    'FileDataLakeStorage',
    'UnsupportedProviderError',
    'data_lake_factory',
]
