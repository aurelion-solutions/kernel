# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityMapping route dependencies."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capability_mappings.service import CapabilityMappingService
from src.core.db.deps import get_db
from src.platform.logs.deps import get_log_service
from src.platform.logs.service import LogService

_DependsDB = Depends(get_db)
_DependsLogs = Depends(get_log_service)


async def get_capability_mapping_service(
    session: AsyncSession = _DependsDB,
    log_service: LogService = _DependsLogs,
) -> CapabilityMappingService:
    """Return a CapabilityMappingService bound to the request session and log service."""
    return CapabilityMappingService(session, log_service)
