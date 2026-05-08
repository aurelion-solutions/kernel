# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the CapabilityGrant slice."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.access_model.capability_grants.service import CapabilityGrantReadService

_DependsDB = Depends(get_db)


async def get_capability_grant_read_service(
    session: AsyncSession = _DependsDB,
) -> CapabilityGrantReadService:
    """Provide a CapabilityGrantReadService for read-only route handlers."""
    return CapabilityGrantReadService(session)
