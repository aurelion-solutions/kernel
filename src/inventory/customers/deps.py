# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Customer route dependencies."""

import os

from src.inventory.customers.service import CustomerService
from src.inventory.subjects.service import SubjectService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService


def _get_events_provider() -> str:
    return os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')


def get_customer_service() -> CustomerService:
    """Return CustomerService with injected EventService; SubjectService shares the same event bus."""
    event_service = EventService(sink=event_sink_factory.get(_get_events_provider()))
    subject_service = SubjectService(event_service=event_service)
    return CustomerService(event_service=event_service, subject_service=subject_service)
