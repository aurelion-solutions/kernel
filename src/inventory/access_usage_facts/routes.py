# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact API routes."""

from __future__ import annotations

from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.access_usage_facts.deps import get_access_usage_fact_service
from src.inventory.access_usage_facts.schemas import AccessUsageFactCreate, AccessUsageFactRead
from src.inventory.access_usage_facts.service import (
    AccessUsageFactDuplicateError,
    AccessUsageFactForeignKeyError,
    AccessUsageFactNotFoundError,
    AccessUsageFactService,
    AccessUsageFactWindowOrderError,
)

router = APIRouter(prefix='/access-usage-facts', tags=['access-usage-facts'])
DependsSession = Depends(get_db)
DependsService = Depends(get_access_usage_fact_service)


@router.get('', response_model=list[AccessUsageFactRead])
async def list_access_usage_facts(
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    access_fact_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = DependsSession,
    service: AccessUsageFactService = DependsService,
) -> list[AccessUsageFactRead]:
    """List access usage facts with optional filters."""
    facts = await service.list_usage_facts(
        session,
        subject_id=subject_id,
        resource_id=resource_id,
        access_fact_id=access_fact_id,
        since=since,
        limit=limit,
        offset=offset,
    )
    return [AccessUsageFactRead.model_validate(f) for f in facts]


@router.get('/{usage_fact_id}', response_model=AccessUsageFactRead)
async def get_access_usage_fact(
    usage_fact_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: AccessUsageFactService = DependsService,
) -> AccessUsageFactRead:
    """Get access usage fact by id."""
    fact = await service.get_usage_fact(session, usage_fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail='Access usage fact not found')
    return AccessUsageFactRead.model_validate(fact)


@router.post('', response_model=AccessUsageFactRead, status_code=201)
async def create_access_usage_fact(
    body: AccessUsageFactCreate,
    session: AsyncSession = DependsSession,
    service: AccessUsageFactService = DependsService,
) -> AccessUsageFactRead:
    """Create a new access usage fact."""
    try:
        fact = await service.create_usage_fact(
            session,
            access_fact_id=body.access_fact_id,
            last_seen=body.last_seen,
            usage_count=body.usage_count,
            window_from=body.window_from,
            window_to=body.window_to,
        )
    except AccessUsageFactForeignKeyError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except AccessUsageFactWindowOrderError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except AccessUsageFactDuplicateError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    except AccessUsageFactNotFoundError as exc:
        raise HTTPException(status_code=404, detail='Access usage fact not found') from exc
    return AccessUsageFactRead.model_validate(fact)
