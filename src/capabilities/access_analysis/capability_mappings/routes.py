# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityMapping API routes.

Endpoints:
  POST   /capability-mappings                        — create
  GET    /capability-mappings                        — list (filters: capability_id, application_id, scope_key_id)
  GET    /capability-mappings/{mapping_id}           — get by id
  PATCH  /capability-mappings/{mapping_id}           — partial update
  DELETE /capability-mappings/{mapping_id}           — hard delete (pending CapabilityGrant in Step 4)
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends
from src.capabilities.access_analysis.capability_mappings.deps import get_capability_mapping_service
from src.capabilities.access_analysis.capability_mappings.exceptions import (
    CapabilityMappingInUseError,
    CapabilityMappingNotFoundError,
    CapabilityMappingResourceMatchExclusivityError,
    CapabilityMappingUnknownActionSlugError,
    CapabilityMappingUnknownApplicationIdError,
    CapabilityMappingUnknownCapabilityIdError,
    CapabilityMappingUnknownResourceIdError,
    CapabilityMappingUnknownScopeKeyIdError,
)
from src.capabilities.access_analysis.capability_mappings.schemas import (
    CapabilityMappingCreate,
    CapabilityMappingPatch,
    CapabilityMappingRead,
)
from src.capabilities.access_analysis.capability_mappings.service import CapabilityMappingService
from src.core.http.errors import translate_service_errors

router = APIRouter(prefix='/capability-mappings', tags=['capability-mappings'])
DependsService = Depends(get_capability_mapping_service)


@router.post('', response_model=CapabilityMappingRead, status_code=201)
async def create_capability_mapping(
    body: CapabilityMappingCreate,
    service: CapabilityMappingService = DependsService,
) -> CapabilityMappingRead:
    """Create a new CapabilityMapping rule."""
    with translate_service_errors(
        {
            CapabilityMappingResourceMatchExclusivityError: (
                422,
                'exactly one of resource_id, resource_kind, resource_path_glob must be set',
            ),
            CapabilityMappingUnknownCapabilityIdError: (
                404,
                lambda e: f'Capability {e.capability_id} not found',
            ),
            CapabilityMappingUnknownApplicationIdError: (
                404,
                lambda e: f'Application {e.application_id} not found',
            ),
            CapabilityMappingUnknownResourceIdError: (
                404,
                lambda e: f'Resource {e.resource_id} not found',
            ),
            CapabilityMappingUnknownScopeKeyIdError: (
                404,
                lambda e: f'Capability scope key {e.scope_key_id} not found',
            ),
            CapabilityMappingUnknownActionSlugError: (
                422,
                lambda e: f"Unknown action slug '{e.action_slug}'",
            ),
        }
    ):
        result = await service.create(body)
    return result


@router.get('', response_model=list[CapabilityMappingRead])
async def list_capability_mappings(
    capability_id: int | None = None,
    application_id: UUID | None = None,
    scope_key_id: int | None = None,
    is_active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    service: CapabilityMappingService = DependsService,
) -> list[CapabilityMappingRead]:
    """List capability mappings with optional filters. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(
        capability_id=capability_id,
        application_id=application_id,
        scope_key_id=scope_key_id,
        is_active=is_active,
        limit=effective_limit,
        offset=offset,
    )


@router.get('/{mapping_id}', response_model=CapabilityMappingRead)
async def get_capability_mapping(
    mapping_id: int,
    service: CapabilityMappingService = DependsService,
) -> CapabilityMappingRead:
    """Get a CapabilityMapping by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            CapabilityMappingNotFoundError: (404, 'Capability mapping not found'),
        }
    ):
        result = await service.get(mapping_id)
    return result


@router.patch('/{mapping_id}', response_model=CapabilityMappingRead)
async def patch_capability_mapping(
    mapping_id: int,
    body: CapabilityMappingPatch,
    service: CapabilityMappingService = DependsService,
) -> CapabilityMappingRead:
    """Partially update a CapabilityMapping. capability_id, application_id, created_by are immutable."""
    payload_dict = body.model_dump(exclude_unset=True)
    with translate_service_errors(
        {
            CapabilityMappingNotFoundError: (404, 'Capability mapping not found'),
            CapabilityMappingResourceMatchExclusivityError: (
                422,
                'exactly one of resource_id, resource_kind, resource_path_glob must be set',
            ),
            CapabilityMappingUnknownActionSlugError: (
                422,
                lambda e: f"Unknown action slug '{e.action_slug}'",
            ),
            CapabilityMappingUnknownScopeKeyIdError: (
                404,
                lambda e: f'Capability scope key {e.scope_key_id} not found',
            ),
            CapabilityMappingUnknownResourceIdError: (
                404,
                lambda e: f'Resource {e.resource_id} not found',
            ),
        }
    ):
        result = await service.patch(mapping_id, payload_dict)
    return result


@router.delete('/{mapping_id}', status_code=204)
async def delete_capability_mapping(
    mapping_id: int,
    service: CapabilityMappingService = DependsService,
) -> None:
    """Hard-delete a CapabilityMapping. Returns 409 if in use by capability grants."""
    with translate_service_errors(
        {
            CapabilityMappingNotFoundError: (404, 'Capability mapping not found'),
            CapabilityMappingInUseError: (
                409,
                lambda e: f'Capability mapping {e.mapping_id} is referenced by {e.grant_count} capability grants',
            ),
        }
    ):
        await service.delete(mapping_id)
