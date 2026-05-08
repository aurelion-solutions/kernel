# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.org_units.lake_service import (
    OrgUnitLakeNotConfiguredError,
    OrgUnitLakeService,
    OrgUnitLakeWriteError,
)
from src.inventory.org_units.repository import list_all_org_units
from src.inventory.org_units.schemas import (
    OrgUnitBulkRequest,
    OrgUnitBulkResponse,
    OrgUnitListItem,
    OrgUnitListResponse,
)

router = APIRouter(prefix='/org-units', tags=['org-units'])
DependsSession = Depends(get_db)


@router.get('', response_model=OrgUnitListResponse, status_code=200)
async def list_org_units(
    session: AsyncSession = DependsSession,
) -> OrgUnitListResponse:
    """Return all org units ordered by external_id ascending."""
    org_units = await list_all_org_units(session)
    return OrgUnitListResponse(
        items=[OrgUnitListItem.model_validate(ou) for ou in org_units],
    )


@router.post('/bulk', response_model=OrgUnitBulkResponse, status_code=200)
async def bulk_upsert_org_units(
    body: OrgUnitBulkRequest,
    request: Request,
) -> OrgUnitBulkResponse:
    """Bulk-ingest org units into the lake (raw.org_units).  PG is populated later via reconcile+apply."""
    lake_catalog = getattr(request.app.state, 'lake_catalog', None)
    service = OrgUnitLakeService(lake_catalog=lake_catalog)
    try:
        result = await service.upsert_batch(body.items, ingest_batch_id=uuid.uuid4())
    except OrgUnitLakeNotConfiguredError:
        raise HTTPException(status_code=503, detail='Lake backend not configured') from None
    except OrgUnitLakeWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return OrgUnitBulkResponse(row_count=result.row_count, snapshot_id=result.snapshot_id)
