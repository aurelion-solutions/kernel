# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Policy service dependency for route injection."""

from src.capabilities.policy.service import PolicyService
from src.platform.logs.deps import get_log_service


def get_policy_service() -> PolicyService:
    """Return PolicyService with injected log service."""
    return PolicyService(log_service=get_log_service())
