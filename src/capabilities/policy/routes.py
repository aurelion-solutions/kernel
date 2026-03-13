# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Policy API routes."""

from fastapi import APIRouter, Depends
from src.capabilities.policy.deps import get_policy_service
from src.capabilities.policy.schemas import Decision, Facts
from src.capabilities.policy.service import PolicyService  # noqa: TCH001

router = APIRouter(prefix='/policy', tags=['policy'])
DependsService = Depends(get_policy_service)


@router.post('/evaluate', response_model=Decision)
def evaluate_policy(
    body: Facts,
    service: PolicyService = DependsService,
) -> Decision:
    """Evaluate policy rules against the provided facts."""
    return service.evaluate_policy(body)
