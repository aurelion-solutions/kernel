# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.inventory.org_units.lake_service import (
    OrgUnitLakeNotConfiguredError,
    OrgUnitLakeService,
    OrgUnitLakeWriteError,
)
from src.inventory.org_units.repository import list_org_units_page
from src.inventory.org_units.schemas import (
    OrgUnitBulkRequest,
    OrgUnitBulkResponse,
    OrgUnitCreate,
    OrgUnitListItem,
    OrgUnitListResponse,
    OrgUnitRead,
    OrgUnitUpdate,
)
from src.inventory.org_units.service import (
    DuplicateExternalIdError,
    InternalOrgUnitImmutableError,
    OrgUnitNotFoundError,
    OrgUnitService,
    ParentMustBeExternalError,
)

router = APIRouter(prefix='/org-units', tags=['org-units'])
DependsSession = Depends(get_db)


@router.get('', response_model=OrgUnitListResponse, status_code=200)
async def list_org_units(
    session: AsyncSession = DependsSession,
    limit: int = Query(..., ge=1, le=1000),
    offset: int = Query(..., ge=0),
) -> OrgUnitListResponse:
    """Return org units ordered by external_id ascending, paginated by limit/offset."""
    org_units, total = await list_org_units_page(session, limit=limit, offset=offset)
    return OrgUnitListResponse(
        items=[OrgUnitListItem.model_validate(ou) for ou in org_units],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post('/bulk', response_model=OrgUnitBulkResponse, status_code=200)
async def bulk_upsert_org_units(
    body: OrgUnitBulkRequest,
    request: Request,
) -> OrgUnitBulkResponse:
    """Bulk-ingest org units into the lake (raw.org_units).  PG is populated later via reconcile+apply.

    K-N note: OrgUnitBulkItem.is_internal is accepted at the Pydantic boundary for
    forward-compatibility but is silently discarded on this path.  The lake-first
    ingest writes to raw.org_units (Iceberg) which has no is_internal column; PG
    receives the column only when reconcile+apply runs.  The only way to persist
    is_internal=False today is a direct OrgUnitService call (tests / seed scripts).
    This asymmetry is intentional for K-N and will be resolved in the step that
    migrates the raw.org_units Iceberg schema and wires apply_org_units_delta.
    """
    lake_catalog = getattr(request.app.state, 'lake_catalog', None)
    service = OrgUnitLakeService(lake_catalog=lake_catalog)
    try:
        result = await service.upsert_batch(body.items, ingest_batch_id=uuid.uuid4())
    except OrgUnitLakeNotConfiguredError:
        raise HTTPException(status_code=503, detail='Lake backend not configured') from None
    except OrgUnitLakeWriteError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return OrgUnitBulkResponse(row_count=result.row_count, snapshot_id=result.snapshot_id)


_svc = OrgUnitService()


@router.post('', response_model=OrgUnitRead, status_code=201)
async def create_org_unit(
    body: OrgUnitCreate,
    session: AsyncSession = DependsSession,
) -> OrgUnitRead:
    """Create a single external org-unit."""
    with translate_service_errors(
        {
            ParentMustBeExternalError: (422, 'parent_id must reference an existing external org-unit'),
            DuplicateExternalIdError: (409, 'An org-unit with this external_id already exists'),
        }
    ):
        org_unit = await _svc.create_external_org_unit(session, body)
    await session.commit()
    return OrgUnitRead.model_validate(org_unit)


@router.get('/{org_unit_id}', response_model=OrgUnitRead, status_code=200)
async def get_org_unit(
    org_unit_id: uuid.UUID,
    session: AsyncSession = DependsSession,
) -> OrgUnitRead:
    """Return a single org-unit by id."""
    with translate_service_errors(
        {
            OrgUnitNotFoundError: (404, 'Org-unit not found'),
        }
    ):
        org_unit = await _svc.read_org_unit(session, org_unit_id)
    return OrgUnitRead.model_validate(org_unit)


@router.patch('/{org_unit_id}', response_model=OrgUnitRead, status_code=200)
async def update_org_unit(
    org_unit_id: uuid.UUID,
    body: OrgUnitUpdate,
    session: AsyncSession = DependsSession,
) -> OrgUnitRead:
    """Update name and/or description of an external org-unit."""
    with translate_service_errors(
        {
            OrgUnitNotFoundError: (404, 'Org-unit not found'),
            InternalOrgUnitImmutableError: (409, 'Internal org-units are reconcile-owned'),
        }
    ):
        org_unit = await _svc.update_external_org_unit(session, org_unit_id, body)
    await session.commit()
    return OrgUnitRead.model_validate(org_unit)


@router.delete('/{org_unit_id}', status_code=204)
async def delete_org_unit(
    org_unit_id: uuid.UUID,
    session: AsyncSession = DependsSession,
) -> Response:
    """Delete an external org-unit. Employees are unbound (org_unit_id → NULL)."""
    with translate_service_errors(
        {
            OrgUnitNotFoundError: (404, 'Org-unit not found'),
            InternalOrgUnitImmutableError: (409, 'Internal org-units are reconcile-owned'),
        }
    ):
        await _svc.delete_external_org_unit(session, org_unit_id)
    await session.commit()
    return Response(status_code=204)
