# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact API routes — read-only (lake-backed, Phase 15 Step 16)."""

from __future__ import annotations

from datetime import datetime
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.inventory.access_facts.deps import get_access_fact_service
from src.inventory.access_facts.display_enrichment import enrich_access_facts
from src.inventory.access_facts.schemas import (
    AccessFactArtifactRefRead,
    AccessFactEffect,
    AccessFactRead,
    AccessFactView,
)
from src.inventory.access_facts.service import AccessFactArtifactRefNotFoundError, AccessFactService
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSession

router = APIRouter(prefix='/access-facts', tags=['access-facts'])
DependsService = Depends(get_access_fact_service)
DependsLakeSession = Depends(get_lake_session)
DependsDB = Depends(get_db)


@router.get('', response_model=list[AccessFactRead])
async def list_access_facts(
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    action_slug: str | None = None,
    effect: AccessFactEffect | None = None,
    is_active: bool | None = None,
    valid_at: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    lake_session: LakeSession = DependsLakeSession,
    service: AccessFactService = DependsService,
    pg_session: AsyncSession = DependsDB,
) -> list[AccessFactRead]:
    """List access facts with optional filters.

    action_slug: filter by action slug (e.g. 'read', 'write'). Unknown slug returns [].
    is_active: filter by lifecycle state (True=active only, False=revoked only, omit=both).
    Display fields (subject_display, account_display, resource_display, application_code)
    are resolved via a single PG batch lookup per entity type — never N+1.
    """
    views = await service.list_facts(
        lake_session,
        subject_id=subject_id,
        resource_id=resource_id,
        account_id=account_id,
        action_slug=action_slug,
        effect=effect,
        is_active=is_active,
        valid_at=valid_at,
        limit=limit,
        offset=offset,
    )
    return await enrich_access_facts(pg_session, views)


@router.get('/{fact_id}', response_model=AccessFactRead)
async def get_access_fact(
    fact_id: uuid.UUID,
    lake_session: LakeSession = DependsLakeSession,
    service: AccessFactService = DependsService,
) -> AccessFactRead:
    """Get access fact by id."""
    view: AccessFactView | None = await service.get_fact(lake_session, fact_id)
    if view is None:
        raise HTTPException(status_code=404, detail='Access fact not found')
    return _view_to_read(view)


@router.get('/{fact_id}/artifact-ref', response_model=AccessFactArtifactRefRead)
async def get_access_fact_artifact_ref(
    fact_id: uuid.UUID,
    lake_session: LakeSession = DependsLakeSession,
    pg_session: AsyncSession = DependsDB,
    service: AccessFactService = DependsService,
) -> AccessFactArtifactRefRead:
    """Resolve the drill-down chain: access_fact → delta_item → access_artifact.

    Returns { artifact_id, application_id, external_id }.
    Returns 404 if any link in the chain is missing or orphaned.
    """
    with translate_service_errors(
        {
            AccessFactArtifactRefNotFoundError: (404, 'Access fact artifact reference not found'),
        }
    ):
        return await service.get_artifact_ref(lake_session, pg_session, fact_id)


def _view_to_read(view: AccessFactView) -> AccessFactRead:
    """Convert AccessFactView DTO to AccessFactRead response schema."""
    return AccessFactRead(
        id=view.id,
        subject_id=view.subject_id,
        account_id=view.account_id,
        resource_id=view.resource_id,
        action_slug=view.action_slug,
        effect=view.effect,
        is_active=view.is_active,
        revoked_at=view.revoked_at,
        observed_at=view.observed_at,
        valid_from=view.valid_from,
        valid_until=view.valid_until,
        created_at=view.created_at,
    )
