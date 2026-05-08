# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Policy API routes."""

from fastapi import APIRouter, Depends
from src.engines.policy_assessment.deps import get_policy_service
from src.engines.policy_assessment.schemas import Decision, Facts
from src.engines.policy_assessment.service import PolicyService  # noqa: TCH001

router = APIRouter(prefix='/policy', tags=['policy'])
DependsService = Depends(get_policy_service)


@router.post('/evaluate', response_model=Decision)
def evaluate_policy(
    body: Facts,
    service: PolicyService = DependsService,
) -> Decision:
    """Evaluate policy rules against the provided facts."""
    return service.evaluate_policy(body)
