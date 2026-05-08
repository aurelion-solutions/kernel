# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Policy service dependency for route injection."""

from fastapi import Request
from src.engines.policy_assessment.service import PolicyService
from src.platform.logs.service import NoOpLogService


def get_policy_service(request: Request) -> PolicyService:
    """Return PolicyService with injected log service from app state."""
    log_service = getattr(request.app.state, 'log_service', None) or NoOpLogService()
    return PolicyService(log_service=log_service)
