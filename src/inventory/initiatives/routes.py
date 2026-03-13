# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.initiatives.deps import get_initiative_service
from src.inventory.initiatives.models import InitiativeType
from src.inventory.initiatives.schemas import InitiativeCreate, InitiativePatch, InitiativeRead
from src.inventory.initiatives.service import (
    InitiativeEmptyPatchError,
    InitiativeForeignKeyError,
    InitiativeNotFoundError,
    InitiativeService,
)

router = APIRouter(prefix='/initiatives', tags=['initiatives'])
DependsSession = Depends(get_db)
DependsService = Depends(get_initiative_service)


@router.get('', response_model=list[InitiativeRead])
async def list_initiatives(
    access_fact_id: uuid.UUID | None = None,
    type: InitiativeType | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = DependsSession,
    service: InitiativeService = DependsService,
) -> list[InitiativeRead]:
    """List initiatives with optional filters."""
    items = await service.list_initiatives(
        session,
        access_fact_id=access_fact_id,
        type_=type,
        limit=limit,
        offset=offset,
    )
    return [InitiativeRead.model_validate(i) for i in items]


@router.get('/{initiative_id}', response_model=InitiativeRead)
async def get_initiative(
    initiative_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: InitiativeService = DependsService,
) -> InitiativeRead:
    """Get initiative by id."""
    initiative = await service.get_initiative(session, initiative_id)
    if initiative is None:
        raise HTTPException(status_code=404, detail='Initiative not found')
    return InitiativeRead.model_validate(initiative)


@router.post('', response_model=InitiativeRead, status_code=201)
async def create_initiative(
    body: InitiativeCreate,
    session: AsyncSession = DependsSession,
    service: InitiativeService = DependsService,
) -> InitiativeRead:
    """Create a new initiative."""
    try:
        initiative = await service.create_initiative(
            session,
            access_fact_id=body.access_fact_id,
            type_=body.type,
            origin=body.origin,
            valid_from=body.valid_from,
            valid_until=body.valid_until,
        )
    except InitiativeForeignKeyError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return InitiativeRead.model_validate(initiative)


@router.patch('/{initiative_id}', response_model=InitiativeRead)
async def update_initiative(
    initiative_id: uuid.UUID,
    body: InitiativePatch,
    session: AsyncSession = DependsSession,
    service: InitiativeService = DependsService,
) -> InitiativeRead:
    """Partially update an initiative."""
    try:
        initiative = await service.update_initiative(session, initiative_id, body)
    except InitiativeNotFoundError as exc:
        raise HTTPException(status_code=404, detail='Initiative not found') from exc
    except InitiativeEmptyPatchError as exc:
        raise HTTPException(status_code=422, detail='At least one field must be provided') from exc
    return InitiativeRead.model_validate(initiative)
