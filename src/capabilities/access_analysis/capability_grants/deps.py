# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the CapabilityGrant slice."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capability_grants.service import CapabilityGrantReadService
from src.core.db.deps import get_db

_DependsDB = Depends(get_db)


async def get_capability_grant_read_service(
    session: AsyncSession = _DependsDB,
) -> CapabilityGrantReadService:
    """Provide a CapabilityGrantReadService for read-only route handlers."""
    return CapabilityGrantReadService(session)
