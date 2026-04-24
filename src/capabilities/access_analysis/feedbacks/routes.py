# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Feedback API routes.

Endpoints:
  POST   /feedbacks              — create (201); feedback is immutable, no PATCH/DELETE
  GET    /feedbacks              — list (filters: kind, rule_id, capability_mapping_id,
                                          finding_id, subject_id, limit, offset)
  GET    /feedbacks/{id}         — get by id (404 if missing)

Error mapping:
  FeedbackTargetMissingError                → 422
  FeedbackRuleNotFoundError                 → 404
  FeedbackCapabilityMappingNotFoundError    → 404
  FeedbackFindingNotFoundError              → 404
  FeedbackSubjectNotFoundError              → 404
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from src.capabilities.access_analysis.feedbacks.deps import get_feedback_service
from src.capabilities.access_analysis.feedbacks.exceptions import (
    FeedbackCapabilityMappingNotFoundError,
    FeedbackFindingNotFoundError,
    FeedbackRuleNotFoundError,
    FeedbackSubjectNotFoundError,
    FeedbackTargetMissingError,
)
from src.capabilities.access_analysis.feedbacks.models import FeedbackKind
from src.capabilities.access_analysis.feedbacks.schemas import FeedbackCreate, FeedbackRead
from src.capabilities.access_analysis.feedbacks.service import FeedbackService
from src.core.http.errors import translate_service_errors

router = APIRouter(prefix='/feedbacks', tags=['feedbacks'])
DependsService = Depends(get_feedback_service)

_ERROR_MAP = {
    FeedbackTargetMissingError: (
        422,
        'At least one of rule_id, capability_mapping_id, or finding_id must be set',
    ),
    FeedbackRuleNotFoundError: (404, 'SodRule not found'),
    FeedbackCapabilityMappingNotFoundError: (404, 'CapabilityMapping not found'),
    FeedbackFindingNotFoundError: (404, 'Finding not found'),
    FeedbackSubjectNotFoundError: (404, 'Subject not found'),
}


@router.post('', response_model=FeedbackRead, status_code=201)
async def create_feedback(
    body: FeedbackCreate,
    service: FeedbackService = DependsService,
) -> FeedbackRead:
    """Create a new Feedback. Feedback is immutable — no PATCH or DELETE endpoints exist."""
    with translate_service_errors(_ERROR_MAP):
        result = await service.create_feedback(body)
    return result


@router.get('', response_model=list[FeedbackRead])
async def list_feedbacks_endpoint(
    kind: FeedbackKind | None = None,
    rule_id: int | None = None,
    capability_mapping_id: int | None = None,
    finding_id: int | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
    service: FeedbackService = DependsService,
) -> list[FeedbackRead]:
    """List feedbacks with optional filters. Ordered by created_at DESC. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list_feedbacks(
        kind=kind,
        rule_id=rule_id,
        capability_mapping_id=capability_mapping_id,
        finding_id=finding_id,
        subject_id=subject_id,
        limit=effective_limit,
        offset=offset,
    )


@router.get('/{feedback_id}', response_model=FeedbackRead)
async def get_feedback(
    feedback_id: int,
    service: FeedbackService = DependsService,
) -> FeedbackRead:
    """Get a Feedback by id. Returns 404 if not found."""
    result = await service.get_feedback_by_id(feedback_id)
    if result is None:
        raise HTTPException(status_code=404, detail='Feedback not found')
    return result
