# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityGrant API routes — read-only.

Endpoints:
  GET /capability-grants          — list with optional filters; capped at 100 rows when
                                    no filter is provided to avoid full-table scans
  GET /capability-grants/{id}     — get by id (404 when missing)

No write endpoints — projection is the only writer (via CapabilityProjectionService).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from src.capabilities.access_analysis.capability_grants.deps import get_capability_grant_read_service
from src.capabilities.access_analysis.capability_grants.exceptions import CapabilityGrantNotFoundError
from src.capabilities.access_analysis.capability_grants.schemas import CapabilityGrantRead
from src.capabilities.access_analysis.capability_grants.service import CapabilityGrantReadService
from src.core.http.errors import translate_service_errors

router = APIRouter(prefix='/capability-grants', tags=['capability-grants'])
DependsService = Depends(get_capability_grant_read_service)


@router.get('', response_model=list[CapabilityGrantRead])
async def list_capability_grants(
    subject_id: UUID | None = None,
    capability_id: int | None = None,
    scope_key_id: int | None = None,
    scope_value: str | None = None,
    application_id: UUID | None = None,
    source_effective_grant_id: UUID | None = None,
    source_capability_mapping_id: int | None = None,
    active_only: bool = True,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    service: CapabilityGrantReadService = DependsService,
) -> list[CapabilityGrantRead]:
    """List capability grants with optional filters.

    When no filter is provided the result is capped at 100 rows to avoid
    unbounded full-table scans. Supply any filter to use the requested limit.
    """
    no_filter = all(
        f is None
        for f in (
            subject_id,
            capability_id,
            application_id,
            source_effective_grant_id,
            source_capability_mapping_id,
        )
    )
    if no_filter:
        raise HTTPException(
            status_code=400,
            detail='At least one filter is required (subject_id, capability_id, application_id, '
            'source_effective_grant_id, or source_capability_mapping_id).',
        )
    effective_limit = limit

    grants = await service.list_grants(
        subject_id=subject_id,
        capability_id=capability_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        application_id=application_id,
        source_effective_grant_id=source_effective_grant_id,
        source_capability_mapping_id=source_capability_mapping_id,
        active_only=active_only,
        limit=effective_limit,
        offset=offset,
    )
    return [CapabilityGrantRead.model_validate(g) for g in grants]


@router.get('/{grant_id}', response_model=CapabilityGrantRead)
async def get_capability_grant(
    grant_id: int,
    service: CapabilityGrantReadService = DependsService,
) -> CapabilityGrantRead:
    """Get a CapabilityGrant by id. Returns 404 if not found."""
    grant = await service.get_grant(grant_id)
    if grant is None:
        with translate_service_errors({CapabilityGrantNotFoundError: (404, 'Capability grant not found')}):
            raise CapabilityGrantNotFoundError(grant_id)
    return CapabilityGrantRead.model_validate(grant)
