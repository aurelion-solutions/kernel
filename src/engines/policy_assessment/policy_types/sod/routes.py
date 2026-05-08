# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SoD Evaluator REST endpoints — POST /sod/evaluate and POST /sod/what-if."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from src.core.http.errors import translate_service_errors
from src.engines.policy_assessment.policy_types.sod.deps import get_sod_evaluator_service
from src.engines.policy_assessment.policy_types.sod.exceptions import (
    WhatIfApplicationNotFoundError,
    WhatIfCapabilityNotFoundError,
    WhatIfScopeKeyNotFoundError,
    WhatIfScopeValueInvalidError,
    WhatIfScopeValueMismatchError,
)
from src.engines.policy_assessment.policy_types.sod.schemas import (
    SodEvaluateRequest,
    SodViolationResponse,
    SodWhatIfRequest,
)
from src.engines.policy_assessment.policy_types.sod.service import SodEvaluatorService

router = APIRouter(prefix='/sod', tags=['sod'])

DependsEvaluator = Depends(get_sod_evaluator_service)


@router.post('/evaluate', response_model=list[SodViolationResponse])
async def evaluate_subject(
    body: SodEvaluateRequest,
    service: SodEvaluatorService = DependsEvaluator,
) -> list[SodViolationResponse]:
    """Evaluate SoD rules for a subject at a given point in time.

    Returns a list of Violation responses. Empty list when subject has no capabilities
    or no enabled rules. A nonexistent subject_id also returns [].

    Never persists — read-only evaluation. No events emitted.
    Default ``at`` is resolved to now(UTC) at the route boundary, never inside the service.
    """
    at = body.at if body.at is not None else datetime.now(UTC)

    with translate_service_errors({}):
        violations = await service.evaluate_subject(subject_id=body.subject_id, at=at)

    return [SodViolationResponse.from_violation(v) for v in violations]


@router.post('/what-if', response_model=list[SodViolationResponse])
async def what_if_subject(
    body: SodWhatIfRequest,
    service: SodEvaluatorService = DependsEvaluator,
) -> list[SodViolationResponse]:
    """Evaluate SoD rules for a subject with synthetic capability overrides.

    Returns a list of Violation responses reflecting what violations would exist if the
    given capability_overrides were added to the subject's current active grants.

    Never persists — read-only evaluation. No events emitted.
    Default ``at`` is resolved to now(UTC) at the route boundary.
    """
    at = body.at if body.at is not None else datetime.now(UTC)

    with translate_service_errors(
        {
            WhatIfCapabilityNotFoundError: (422, lambda e: str(e)),
            WhatIfScopeKeyNotFoundError: (422, lambda e: str(e)),
            WhatIfApplicationNotFoundError: (422, lambda e: str(e)),
            WhatIfScopeValueMismatchError: (422, lambda e: str(e)),
            WhatIfScopeValueInvalidError: (422, lambda e: str(e)),
        }
    ):
        violations = await service.what_if_subject(
            subject_id=body.subject_id,
            at=at,
            capability_overrides=body.capability_overrides,
        )

    return [SodViolationResponse.from_violation(v) for v in violations]
