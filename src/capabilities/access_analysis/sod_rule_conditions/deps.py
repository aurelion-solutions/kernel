# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRuleCondition route dependencies."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.sod_rule_conditions.service import SodRuleConditionService
from src.core.db.deps import get_db
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


async def get_sod_rule_condition_service(
    session: AsyncSession = _DependsDB,
    log_service: LogService = _DependsLogs,
) -> SodRuleConditionService:
    """Return a SodRuleConditionService bound to the request session and log service."""
    return SodRuleConditionService(session, log_service)
