# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRule route dependencies."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.sod_rules.service import SodRuleService
from src.core.db.deps import get_db
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


async def get_sod_rule_service(
    session: AsyncSession = _DependsDB,
    log_service: LogService = _DependsLogs,
) -> SodRuleService:
    """Return a SodRuleService bound to the request session and log service."""
    return SodRuleService(session, log_service)
