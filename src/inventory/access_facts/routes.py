# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact API routes — read-only."""

from __future__ import annotations

from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.access_facts.deps import get_access_fact_service
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.access_facts.schemas import AccessFactRead
from src.inventory.access_facts.service import AccessFactService
from src.inventory.enums import Action

router = APIRouter(prefix='/access-facts', tags=['access-facts'])
DependsSession = Depends(get_db)
DependsService = Depends(get_access_fact_service)


@router.get('', response_model=list[AccessFactRead])
async def list_access_facts(
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    action: Action | None = None,
    effect: AccessFactEffect | None = None,
    valid_at: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = DependsSession,
    service: AccessFactService = DependsService,
) -> list[AccessFactRead]:
    """List access facts with optional filters."""
    facts = await service.list_facts(
        session,
        subject_id=subject_id,
        resource_id=resource_id,
        account_id=account_id,
        action=action,
        effect=effect,
        valid_at=valid_at,
        limit=limit,
        offset=offset,
    )
    return [AccessFactRead.model_validate(f) for f in facts]


@router.get('/{fact_id}', response_model=AccessFactRead)
async def get_access_fact(
    fact_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: AccessFactService = DependsService,
) -> AccessFactRead:
    """Get access fact by id."""
    fact = await service.get_fact(session, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail='Access fact not found')
    return AccessFactRead.model_validate(fact)
