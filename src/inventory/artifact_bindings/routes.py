# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding API routes — read-only."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.artifact_bindings.deps import get_artifact_binding_service
from src.inventory.artifact_bindings.schemas import ArtifactBindingRead
from src.inventory.artifact_bindings.service import ArtifactBindingService

router = APIRouter(prefix='/artifact-bindings', tags=['artifact-bindings'])
DependsSession = Depends(get_db)
DependsService = Depends(get_artifact_binding_service)


@router.get('', response_model=list[ArtifactBindingRead])
async def list_artifact_bindings(
    artifact_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = DependsSession,
    service: ArtifactBindingService = DependsService,
) -> list[ArtifactBindingRead]:
    """List artifact bindings with optional filters.

    Unknown target_type values return an empty list (read-side permissiveness per Q7).
    """
    bindings = await service.list_bindings(
        session,
        artifact_id=artifact_id,
        target_type=target_type,
        target_id=target_id,
        limit=limit,
        offset=offset,
    )
    return [ArtifactBindingRead.model_validate(b) for b in bindings]


@router.get('/{binding_id}', response_model=ArtifactBindingRead)
async def get_artifact_binding(
    binding_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: ArtifactBindingService = DependsService,
) -> ArtifactBindingRead:
    """Get artifact binding by id."""
    binding = await service.get_binding(session, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail='Artifact binding not found')
    return ArtifactBindingRead.model_validate(binding)
