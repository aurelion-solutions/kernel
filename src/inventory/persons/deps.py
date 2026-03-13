# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person route dependencies."""

from src.inventory.persons.service import PersonService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_person_service() -> PersonService:
    """Return PersonService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return PersonService(log_service=log_service)
