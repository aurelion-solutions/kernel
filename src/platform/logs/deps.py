# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Log service dependency for route injection."""

from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_log_service() -> LogService:
    """Return LogService with default factory."""
    return LogService(factory=log_sink_factory)
