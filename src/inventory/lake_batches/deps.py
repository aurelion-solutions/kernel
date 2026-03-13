# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch route dependencies."""

from src.inventory.lake_batches.service import LakeBatchService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService
from src.platform.storage.factory import data_lake_factory


def get_lake_batch_service() -> LakeBatchService:
    """Return LakeBatchService with injected data_lake_factory and log service."""
    log_service = LogService(factory=log_sink_factory)
    return LakeBatchService(
        storage_factory=data_lake_factory,
        log_service=log_service,
    )
