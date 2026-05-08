# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Policy catalog API routes.

Endpoints:
  GET /policies/catalog  — unified product-neutral list of all policies.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.policy.catalog.deps import get_policy_catalog_service
from src.inventory.policy.catalog.schemas import PolicyCatalogResponse
from src.inventory.policy.catalog.service import PolicyCatalogService

router = APIRouter(prefix='/policies', tags=['policies'])
DependsSession = Depends(get_db)
DependsService = Depends(get_policy_catalog_service)


@router.get('/catalog', response_model=PolicyCatalogResponse)
async def get_policy_catalog(
    session: AsyncSession = DependsSession,
    service: PolicyCatalogService = DependsService,
) -> PolicyCatalogResponse:
    """Return the unified policy catalog.

    Combines DB-backed SoD policies with file-backed Lens cartridges into one
    product-neutral list. Read-only — does not mutate state, does not emit
    events, does not change scan or assessment behaviour.
    """
    return await service.get_catalog(session)
