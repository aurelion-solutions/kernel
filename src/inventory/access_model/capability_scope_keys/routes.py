# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityScopeKey API routes.

Endpoints:
  POST   /capability-scope-keys                         — create
  GET    /capability-scope-keys                         — list (filter by is_active)
  GET    /capability-scope-keys/{scope_key_id}          — get by id
  PATCH  /capability-scope-keys/{scope_key_id}          — partial update (code immutable)
  POST   /capability-scope-keys/{scope_key_id}/deactivate — soft-delete

No DELETE /capability-scope-keys/{id} — physical deletion is not supported.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from src.core.http.errors import translate_service_errors
from src.inventory.access_model.capability_scope_keys.deps import get_capability_scope_key_service
from src.inventory.access_model.capability_scope_keys.exceptions import (
    CapabilityScopeKeyCodeAlreadyExistsError,
    CapabilityScopeKeyNotFoundError,
)
from src.inventory.access_model.capability_scope_keys.schemas import (
    CapabilityScopeKeyCreate,
    CapabilityScopeKeyPatch,
    CapabilityScopeKeyRead,
)
from src.inventory.access_model.capability_scope_keys.service import CapabilityScopeKeyService

router = APIRouter(prefix='/capability-scope-keys', tags=['capability-scope-keys'])
DependsService = Depends(get_capability_scope_key_service)


@router.post('', response_model=CapabilityScopeKeyRead, status_code=201)
async def create_capability_scope_key(
    body: CapabilityScopeKeyCreate,
    service: CapabilityScopeKeyService = DependsService,
) -> CapabilityScopeKeyRead:
    """Create a new CapabilityScopeKey vocabulary entry."""
    with translate_service_errors(
        {
            CapabilityScopeKeyCodeAlreadyExistsError: (
                409,
                lambda e: f"Capability scope key with code '{e.code}' already exists",
            ),
        }
    ):
        result = await service.create(body)
    return result


@router.get('', response_model=list[CapabilityScopeKeyRead])
async def list_capability_scope_keys(
    is_active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
    service: CapabilityScopeKeyService = DependsService,
) -> list[CapabilityScopeKeyRead]:
    """List capability scope keys, optionally filtered by is_active. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(is_active=is_active, limit=effective_limit, offset=offset)


@router.get('/{scope_key_id}', response_model=CapabilityScopeKeyRead)
async def get_capability_scope_key(
    scope_key_id: int,
    service: CapabilityScopeKeyService = DependsService,
) -> CapabilityScopeKeyRead:
    """Get a CapabilityScopeKey by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            CapabilityScopeKeyNotFoundError: (404, 'Capability scope key not found'),
        }
    ):
        result = await service.get(scope_key_id)
    return result


@router.patch('/{scope_key_id}', response_model=CapabilityScopeKeyRead)
async def patch_capability_scope_key(
    scope_key_id: int,
    body: CapabilityScopeKeyPatch,
    service: CapabilityScopeKeyService = DependsService,
) -> CapabilityScopeKeyRead:
    """Partially update a CapabilityScopeKey. Code is immutable and cannot be changed."""
    with translate_service_errors(
        {
            CapabilityScopeKeyNotFoundError: (404, 'Capability scope key not found'),
        }
    ):
        result = await service.patch(scope_key_id, body)
    return result


@router.post('/{scope_key_id}/deactivate', response_model=CapabilityScopeKeyRead)
async def deactivate_capability_scope_key(
    scope_key_id: int,
    service: CapabilityScopeKeyService = DependsService,
) -> CapabilityScopeKeyRead:
    """Soft-delete a CapabilityScopeKey by setting is_active=False. Idempotent."""
    with translate_service_errors(
        {
            CapabilityScopeKeyNotFoundError: (404, 'Capability scope key not found'),
        }
    ):
        result = await service.deactivate(scope_key_id)
    return result
