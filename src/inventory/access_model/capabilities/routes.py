# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Capability API routes.

Endpoints:
  POST   /capabilities                      — create
  GET    /capabilities                      — list (filter by is_active)
  GET    /capabilities/{capability_id}      — get by id
  PATCH  /capabilities/{capability_id}      — partial update (slug immutable)
  POST   /capabilities/{capability_id}/deactivate — soft-delete

No DELETE /capabilities/{id} — physical deletion is not supported.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from src.core.http.errors import translate_service_errors
from src.inventory.access_model.capabilities.deps import get_capability_service
from src.inventory.access_model.capabilities.exceptions import (
    CapabilityNotFoundError,
    CapabilitySlugAlreadyExistsError,
)
from src.inventory.access_model.capabilities.schemas import (
    CapabilityCreate,
    CapabilityPatch,
    CapabilityRead,
)
from src.inventory.access_model.capabilities.service import CapabilityService

router = APIRouter(prefix='/capabilities', tags=['capabilities'])
DependsService = Depends(get_capability_service)


@router.post('', response_model=CapabilityRead, status_code=201)
async def create_capability(
    body: CapabilityCreate,
    service: CapabilityService = DependsService,
) -> CapabilityRead:
    """Create a new Capability vocabulary entry."""
    with translate_service_errors(
        {
            CapabilitySlugAlreadyExistsError: (
                409,
                lambda e: f"Capability with slug '{e.slug}' already exists",
            ),
        }
    ):
        result = await service.create(body)
    return result


@router.get('', response_model=list[CapabilityRead])
async def list_capabilities(
    is_active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    service: CapabilityService = DependsService,
) -> list[CapabilityRead]:
    """List capabilities, optionally filtered by is_active. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(is_active=is_active, limit=effective_limit, offset=offset)


@router.get('/{capability_id}', response_model=CapabilityRead)
async def get_capability(
    capability_id: int,
    service: CapabilityService = DependsService,
) -> CapabilityRead:
    """Get a Capability by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            CapabilityNotFoundError: (404, 'Capability not found'),
        }
    ):
        result = await service.get(capability_id)
    return result


@router.patch('/{capability_id}', response_model=CapabilityRead)
async def patch_capability(
    capability_id: int,
    body: CapabilityPatch,
    service: CapabilityService = DependsService,
) -> CapabilityRead:
    """Partially update a Capability. Slug is immutable and cannot be changed."""
    with translate_service_errors(
        {
            CapabilityNotFoundError: (404, 'Capability not found'),
        }
    ):
        result = await service.patch(capability_id, body)
    return result


@router.post('/{capability_id}/deactivate', response_model=CapabilityRead)
async def deactivate_capability(
    capability_id: int,
    service: CapabilityService = DependsService,
) -> CapabilityRead:
    """Soft-delete a Capability by setting is_active=False. Idempotent."""
    with translate_service_errors(
        {
            CapabilityNotFoundError: (404, 'Capability not found'),
        }
    ):
        result = await service.deactivate(capability_id)
    return result
