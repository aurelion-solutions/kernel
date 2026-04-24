# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationControl API routes.

Endpoints:
  POST   /mitigation-controls                      — create
  GET    /mitigation-controls                      — list (filter by is_active, type)
  GET    /mitigation-controls/{id}                 — get by id
  PATCH  /mitigation-controls/{id}                 — partial update (code immutable)
  POST   /mitigation-controls/{id}/deactivate      — soft-delete

No DELETE /mitigation-controls/{id} — physical deletion is not supported.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from src.capabilities.access_analysis.mitigation_controls.deps import get_mitigation_control_service
from src.capabilities.access_analysis.mitigation_controls.exceptions import (
    MitigationControlCodeAlreadyExistsError,
    MitigationControlNotFoundError,
)
from src.capabilities.access_analysis.mitigation_controls.models import MitigationControlType
from src.capabilities.access_analysis.mitigation_controls.schemas import (
    MitigationControlCreate,
    MitigationControlPatch,
    MitigationControlRead,
)
from src.capabilities.access_analysis.mitigation_controls.service import MitigationControlService
from src.core.http.errors import translate_service_errors

router = APIRouter(prefix='/mitigation-controls', tags=['mitigation-controls'])
DependsService = Depends(get_mitigation_control_service)


@router.post('', response_model=MitigationControlRead, status_code=201)
async def create_mitigation_control(
    body: MitigationControlCreate,
    service: MitigationControlService = DependsService,
) -> MitigationControlRead:
    """Create a new MitigationControl catalog entry."""
    with translate_service_errors(
        {
            MitigationControlCodeAlreadyExistsError: (
                409,
                lambda e: f"MitigationControl with code '{e.code}' already exists",
            ),
        }
    ):
        result = await service.create(body)
    return result


@router.get('', response_model=list[MitigationControlRead])
async def list_mitigation_controls(
    is_active: bool | None = None,
    type: MitigationControlType | None = None,
    limit: int = 100,
    offset: int = 0,
    service: MitigationControlService = DependsService,
) -> list[MitigationControlRead]:
    """List mitigation controls, optionally filtered by is_active and/or type. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(is_active=is_active, type=type, limit=effective_limit, offset=offset)


@router.get('/{control_id}', response_model=MitigationControlRead)
async def get_mitigation_control(
    control_id: int,
    service: MitigationControlService = DependsService,
) -> MitigationControlRead:
    """Get a MitigationControl by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            MitigationControlNotFoundError: (404, 'MitigationControl not found'),
        }
    ):
        result = await service.get(control_id)
    return result


@router.patch('/{control_id}', response_model=MitigationControlRead)
async def patch_mitigation_control(
    control_id: int,
    body: MitigationControlPatch,
    service: MitigationControlService = DependsService,
) -> MitigationControlRead:
    """Partially update a MitigationControl. Code is immutable and cannot be changed."""
    with translate_service_errors(
        {
            MitigationControlNotFoundError: (404, 'MitigationControl not found'),
        }
    ):
        result = await service.patch(control_id, body)
    return result


@router.post('/{control_id}/deactivate', response_model=MitigationControlRead)
async def deactivate_mitigation_control(
    control_id: int,
    service: MitigationControlService = DependsService,
) -> MitigationControlRead:
    """Soft-delete a MitigationControl by setting is_active=False. Idempotent."""
    with translate_service_errors(
        {
            MitigationControlNotFoundError: (404, 'MitigationControl not found'),
        }
    ):
        result = await service.deactivate(control_id)
    return result
