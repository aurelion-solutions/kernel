# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Secret route dependencies."""

import os

from fastapi import Request
from src.inventory.secrets.service import SecretService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.logs.deps import get_log_service
from src.platform.secrets.factory import secret_manager_factory


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_secret_service(request: Request) -> SecretService:
    """Return SecretService with injected log and event services."""
    return SecretService(
        factory=secret_manager_factory,
        log_service=get_log_service(request),
        event_service=EventService(sink=event_sink_factory.get(_get_events_provider())),
    )
