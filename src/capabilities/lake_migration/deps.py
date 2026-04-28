# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependency providers for the lake_migration slice."""

from __future__ import annotations

from fastapi import Request
from src.capabilities.lake_migration.service import LakeMigrationService
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.logs.deps import get_log_service
from src.platform.storage.factory import DataLakeStorageFactory


def _get_lake_batch_service(request: Request) -> LakeBatchService:
    log_service = get_log_service(request)
    # Use the same storage factory as the rest of the app if available.
    storage_factory = getattr(request.app.state, 'storage_factory', DataLakeStorageFactory())
    return LakeBatchService(storage_factory=storage_factory, log_service=log_service)


async def get_lake_migration_service(
    request: Request,
) -> LakeMigrationService:
    """Build LakeMigrationService with all DI wired."""
    log_service = get_log_service(request)
    lake_batch_service = _get_lake_batch_service(request)
    return LakeMigrationService(
        log_service=log_service,
        lake_batch_service=lake_batch_service,
    )
