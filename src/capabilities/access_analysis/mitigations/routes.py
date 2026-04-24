# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Mitigation API routes.

Endpoints:
  POST   /mitigations                    — create (201)
  GET    /mitigations                    — list (filters: rule_id, subject_id, status, control_id, owner_id)
  GET    /mitigations/{id}               — get by id (404 if missing)
  POST   /mitigations/{id}/activate      — transition proposed → active (409 if not proposed)
  POST   /mitigations/{id}/revoke        — transition to revoked with mandatory reason
  PATCH  /mitigations/{id}/status        — generic status transition

Error mapping:
  MitigationNotFoundError             → 404
  MitigationRuleNotFoundError         → 404
  MitigationRuleNotMitigatableError   → 422
  MitigationControlNotFoundError      → 404
  MitigationControlInactiveError      → 422
  MitigationSubjectNotFoundError      → 404
  MitigationOwnerNotFoundError        → 404
  MitigationScopePairError            → 422
  MitigationValidWindowError          → 422
  MitigationInvalidInitialStatusError → 422
  MitigationDuplicateActiveError      → 409
  MitigationStatusTransitionError     → 409
  MitigationReasonRequiredError       → 422
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from fastapi import APIRouter, Depends
from src.capabilities.access_analysis.mitigations.deps import get_mitigation_service
from src.capabilities.access_analysis.mitigations.exceptions import (
    MitigationControlInactiveError,
    MitigationControlNotFoundError,
    MitigationDuplicateActiveError,
    MitigationInvalidInitialStatusError,
    MitigationNotFoundError,
    MitigationOwnerNotFoundError,
    MitigationReasonRequiredError,
    MitigationRuleNotFoundError,
    MitigationRuleNotMitigatableError,
    MitigationScopePairError,
    MitigationStatusTransitionError,
    MitigationSubjectNotFoundError,
    MitigationValidWindowError,
)
from src.capabilities.access_analysis.mitigations.models import MitigationStatus
from src.capabilities.access_analysis.mitigations.schemas import (
    MitigationCreate,
    MitigationRead,
    MitigationRevokeBody,
    MitigationStatusPatch,
)
from src.capabilities.access_analysis.mitigations.service import MitigationService
from src.core.http.errors import translate_service_errors

router = APIRouter(prefix='/mitigations', tags=['mitigations'])
DependsService = Depends(get_mitigation_service)

_ERROR_MAP = {
    MitigationNotFoundError: (404, 'Mitigation not found'),
    MitigationRuleNotFoundError: (404, 'SodRule not found'),
    MitigationControlNotFoundError: (404, 'MitigationControl not found'),
    MitigationSubjectNotFoundError: (404, 'Subject not found'),
    MitigationOwnerNotFoundError: (404, 'Owner subject not found'),
    MitigationDuplicateActiveError: (409, 'An active or proposed mitigation already exists for this scope tuple'),
    MitigationStatusTransitionError: (
        409,
        lambda e: f'Cannot transition mitigation from {e.current!r} to {e.requested!r}',  # type: ignore[union-attr]
    ),
    MitigationRuleNotMitigatableError: (422, 'SodRule does not allow mitigation'),
    MitigationControlInactiveError: (422, 'MitigationControl is not active'),
    MitigationScopePairError: (422, 'scope_key_id and scope_value must both be set or both be null'),
    MitigationValidWindowError: (422, 'valid_until must be strictly after valid_from'),
    MitigationInvalidInitialStatusError: (422, 'Initial status must be proposed or active'),
    MitigationReasonRequiredError: (422, 'reason is required when revoking a mitigation'),
}


@router.post('', response_model=MitigationRead, status_code=201)
async def create_mitigation(
    body: MitigationCreate,
    service: MitigationService = DependsService,
) -> MitigationRead:
    """Create a new Mitigation. valid_from defaults to now() if not supplied."""
    # Apply API-level default for valid_from — service stays referentially transparent
    if body.valid_from is None:  # type: ignore[truthy-bool]
        body = body.model_copy(update={'valid_from': datetime.now(UTC)})
    with translate_service_errors(_ERROR_MAP):
        result = await service.create(body)
    return result


@router.get('', response_model=list[MitigationRead])
async def list_mitigations_endpoint(
    rule_id: int | None = None,
    subject_id: uuid.UUID | None = None,
    status: MitigationStatus | None = None,
    control_id: int | None = None,
    owner_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
    service: MitigationService = DependsService,
) -> list[MitigationRead]:
    """List mitigations with optional filters. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(
        rule_id=rule_id,
        subject_id=subject_id,
        status=status,
        control_id=control_id,
        owner_id=owner_id,
        limit=effective_limit,
        offset=offset,
    )


@router.get('/{mitigation_id}', response_model=MitigationRead)
async def get_mitigation(
    mitigation_id: int,
    service: MitigationService = DependsService,
) -> MitigationRead:
    """Get a Mitigation by id. Returns 404 if not found."""
    with translate_service_errors(_ERROR_MAP):
        result = await service.get(mitigation_id)
    return result


@router.post('/{mitigation_id}/activate', response_model=MitigationRead)
async def activate_mitigation(
    mitigation_id: int,
    service: MitigationService = DependsService,
) -> MitigationRead:
    """Transition a proposed Mitigation to active. Returns 409 if not in proposed state.

    Not idempotent: re-activating would silently drop the mitigation.activated event,
    breaking the audit trail. Returns 409 on any invalid transition.
    """
    with translate_service_errors(_ERROR_MAP):
        result = await service.patch_status(
            mitigation_id,
            MitigationStatusPatch(status=MitigationStatus.active),
        )
    return result


@router.post('/{mitigation_id}/revoke', response_model=MitigationRead)
async def revoke_mitigation(
    mitigation_id: int,
    body: MitigationRevokeBody,
    service: MitigationService = DependsService,
) -> MitigationRead:
    """Revoke a Mitigation. reason is mandatory."""
    with translate_service_errors(_ERROR_MAP):
        result = await service.patch_status(
            mitigation_id,
            MitigationStatusPatch(status=MitigationStatus.revoked, reason=body.reason),
        )
    return result


@router.patch('/{mitigation_id}/status', response_model=MitigationRead)
async def patch_mitigation_status(
    mitigation_id: int,
    body: MitigationStatusPatch,
    service: MitigationService = DependsService,
) -> MitigationRead:
    """Generic status transition endpoint."""
    with translate_service_errors(_ERROR_MAP):
        result = await service.patch_status(mitigation_id, body)
    return result
