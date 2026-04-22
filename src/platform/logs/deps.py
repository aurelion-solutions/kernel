# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Log service dependency for route injection."""

from fastapi import Request
from src.platform.logs.service import LogService, NoOpLogService


def get_log_service(request: Request) -> LogService:
    """Return the app-scoped LogService singleton, falling back to NoOp in tests."""
    return getattr(request.app.state, 'log_service', None) or NoOpLogService()
