# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact API routes — read-only."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.access_artifacts.deps import get_access_artifact_service
from src.inventory.access_artifacts.schemas import AccessArtifactRead
from src.inventory.access_artifacts.service import AccessArtifactService

router = APIRouter(prefix='/access-artifacts', tags=['access-artifacts'])
DependsSession = Depends(get_db)
DependsService = Depends(get_access_artifact_service)


@router.get('', response_model=list[AccessArtifactRead])
async def list_access_artifacts(
    application_id: uuid.UUID | None = None,
    artifact_type: str | None = None,
    is_active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = DependsSession,
    service: AccessArtifactService = DependsService,
) -> list[AccessArtifactRead]:
    """List access artifacts with optional filters."""
    artifacts = await service.list_artifacts(
        session,
        application_id=application_id,
        artifact_type=artifact_type,
        is_active=is_active,
        limit=limit,
        offset=offset,
    )
    return [AccessArtifactRead.model_validate(a) for a in artifacts]


@router.get('/{artifact_id}', response_model=AccessArtifactRead)
async def get_access_artifact(
    artifact_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: AccessArtifactService = DependsService,
) -> AccessArtifactRead:
    """Get access artifact by id."""
    artifact = await service.get_artifact(session, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail='Access artifact not found')
    return AccessArtifactRead.model_validate(artifact)
