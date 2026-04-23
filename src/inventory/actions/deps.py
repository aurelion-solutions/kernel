# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Action route dependencies."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.actions.service import ActionService
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


def get_action_service(
    session: AsyncSession = _DependsDB,
    log_service: LogService = _DependsLogs,
) -> ActionService:
    """Return an ActionService bound to the request session and log service."""
    return ActionService(session, log_service)
