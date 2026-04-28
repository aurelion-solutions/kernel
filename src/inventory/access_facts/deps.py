# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact route dependencies.

Phase 15 Step 16: event_service removed. Service is lake-read-only.
"""

from fastapi import Request
from src.inventory.access_facts.service import AccessFactService


def get_access_fact_service(request: Request) -> AccessFactService:
    """Return AccessFactService with injected LogService."""
    log_service = getattr(request.app.state, 'log_service', None)
    return AccessFactService(log_service=log_service)
