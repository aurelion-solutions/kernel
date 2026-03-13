# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OwnershipAssignment API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.ownership_assignments.deps import get_ownership_assignment_service
from src.inventory.ownership_assignments.schemas import (
    OwnershipAssignmentCreate,
    OwnershipAssignmentRead,
    OwnershipKind,
)
from src.inventory.ownership_assignments.service import (
    OwnershipAssignmentDuplicateError,
    OwnershipAssignmentForeignKeyError,
    OwnershipAssignmentNotFoundError,
    OwnershipAssignmentService,
    OwnershipAssignmentTargetRequiredError,
)

router = APIRouter(prefix='/ownership-assignments', tags=['ownership-assignments'])
DependsSession = Depends(get_db)
DependsService = Depends(get_ownership_assignment_service)


@router.get('', response_model=list[OwnershipAssignmentRead])
async def list_ownership_assignments(
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    kind: OwnershipKind | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = DependsSession,
    service: OwnershipAssignmentService = DependsService,
) -> list[OwnershipAssignmentRead]:
    """List ownership assignments with optional filters."""
    assignments = await service.list_assignments(
        session,
        subject_id=subject_id,
        resource_id=resource_id,
        account_id=account_id,
        kind=kind,
        limit=limit,
        offset=offset,
    )
    return [OwnershipAssignmentRead.model_validate(a) for a in assignments]


@router.get('/{assignment_id}', response_model=OwnershipAssignmentRead)
async def get_ownership_assignment(
    assignment_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: OwnershipAssignmentService = DependsService,
) -> OwnershipAssignmentRead:
    """Get ownership assignment by id."""
    assignment = await service.get_assignment(session, assignment_id)
    if assignment is None:
        raise HTTPException(status_code=404, detail='Ownership assignment not found')
    return OwnershipAssignmentRead.model_validate(assignment)


@router.post('', response_model=OwnershipAssignmentRead, status_code=201)
async def create_ownership_assignment(
    body: OwnershipAssignmentCreate,
    session: AsyncSession = DependsSession,
    service: OwnershipAssignmentService = DependsService,
) -> OwnershipAssignmentRead:
    """Create a new ownership assignment."""
    try:
        assignment = await service.create_assignment(
            session,
            subject_id=body.subject_id,
            resource_id=body.resource_id,
            account_id=body.account_id,
            kind=body.kind,
        )
    except OwnershipAssignmentForeignKeyError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except OwnershipAssignmentTargetRequiredError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except OwnershipAssignmentDuplicateError as exc:
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    return OwnershipAssignmentRead.model_validate(assignment)


@router.delete('/{assignment_id}', status_code=204)
async def delete_ownership_assignment(
    assignment_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: OwnershipAssignmentService = DependsService,
) -> Response:
    """Delete an ownership assignment."""
    try:
        await service.delete_assignment(session, assignment_id)
    except OwnershipAssignmentNotFoundError as exc:
        raise HTTPException(status_code=404, detail='Ownership assignment not found') from exc
    return Response(status_code=204)
