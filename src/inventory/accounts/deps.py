# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account route dependencies."""

from src.inventory.accounts.service import AccountService
from src.platform.logs.factory import log_sink_factory
from src.platform.logs.service import LogService


def get_account_service() -> AccountService:
    """Return AccountService with injected log service."""
    log_service = LogService(factory=log_sink_factory)
    return AccountService(log_service=log_service)
