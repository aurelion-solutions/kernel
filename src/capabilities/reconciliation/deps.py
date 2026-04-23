# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ReconciliationService FastAPI dependency."""

from __future__ import annotations

import os

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.service import ReconciliationService
from src.inventory.access_facts.service import AccessFactService
from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_reconciliation_service(session: AsyncSession) -> ReconciliationService:
    """Build ReconciliationService with all sub-service dependencies wired."""
    event_sink = event_sink_factory.get(_get_events_provider())
    event_service = EventService(sink=event_sink)
    access_fact_service = AccessFactService(event_service=event_service)
    artifact_binding_service = ArtifactBindingService(event_service=event_service)
    return ReconciliationService(
        session=session,
        events=event_service,
        access_fact_service=access_fact_service,
        artifact_binding_service=artifact_binding_service,
    )
