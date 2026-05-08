# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationControl route dependencies."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.assessment.mitigation_controls.service import MitigationControlService
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


async def get_mitigation_control_service(
    session: AsyncSession = _DependsDB,
    log_service: LogService = _DependsLogs,
) -> MitigationControlService:
    """Return a MitigationControlService bound to the request session and log service."""
    return MitigationControlService(session, log_service)
