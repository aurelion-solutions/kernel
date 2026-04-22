# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch route dependencies."""

import os

from fastapi import Request
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.logs.deps import get_log_service
from src.platform.storage.factory import data_lake_factory


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_lake_batch_service(request: Request) -> LakeBatchService:
    """Return LakeBatchService with injected log and event services."""
    event_sink = event_sink_factory.get(_get_events_provider())
    return LakeBatchService(
        storage_factory=data_lake_factory,
        log_service=get_log_service(request),
        event_service=EventService(sink=event_sink),
    )
