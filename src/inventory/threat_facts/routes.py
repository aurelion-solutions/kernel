# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ThreatFact API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.threat_facts.deps import get_threat_fact_service
from src.inventory.threat_facts.schemas import ThreatFactRead, ThreatFactUpsert
from src.inventory.threat_facts.service import (
    ThreatFactAccountNotFoundError,
    ThreatFactConflictError,
    ThreatFactService,
    ThreatFactSubjectNotFoundError,
)

router = APIRouter(prefix='/threat-facts', tags=['threat-facts'])
DependsSession = Depends(get_db)
DependsService = Depends(get_threat_fact_service)


@router.get('', response_model=list[ThreatFactRead])
async def list_threat_facts(
    subject_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    min_risk_score: float | None = Query(default=None, ge=0.0, le=1.0),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = DependsSession,
    service: ThreatFactService = DependsService,
) -> list[ThreatFactRead]:
    """List threat facts with optional filters."""
    facts = await service.list_threat_facts(
        session,
        subject_id=subject_id,
        account_id=account_id,
        min_risk_score=min_risk_score,
        limit=limit,
        offset=offset,
    )
    return [ThreatFactRead.model_validate(f) for f in facts]


@router.get('/{fact_id}', response_model=ThreatFactRead)
async def get_threat_fact(
    fact_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: ThreatFactService = DependsService,
) -> ThreatFactRead:
    """Get threat fact by id."""
    fact = await service.get_threat_fact(session, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail='Threat fact not found')
    return ThreatFactRead.model_validate(fact)


@router.put('/{subject_id}', response_model=ThreatFactRead, status_code=200)
async def upsert_threat_fact(
    subject_id: uuid.UUID,
    body: ThreatFactUpsert,
    response: Response,
    session: AsyncSession = DependsSession,
    service: ThreatFactService = DependsService,
) -> ThreatFactRead:
    """Upsert threat fact for a subject. Returns 201 on first insert, 200 on update."""
    try:
        fact, created = await service.upsert_threat_fact(
            session,
            subject_id=subject_id,
            payload=body,
        )
    except ThreatFactSubjectNotFoundError as exc:
        raise HTTPException(status_code=422, detail=f'Subject not found: {exc.subject_id}') from exc
    except ThreatFactAccountNotFoundError as exc:
        raise HTTPException(status_code=422, detail=f'Account not found: {exc.account_id}') from exc
    except ThreatFactConflictError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if created:
        response.status_code = 201
    return ThreatFactRead.model_validate(fact)
