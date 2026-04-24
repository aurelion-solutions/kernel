# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for SoD endpoints."""

from __future__ import annotations

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.services.capability_resolver import CapabilityResolverService
from src.core.db.deps import get_db

_DependsDB = Depends(get_db)


async def get_capability_resolver_service(
    session: AsyncSession = _DependsDB,
) -> CapabilityResolverService:
    return CapabilityResolverService(session)
