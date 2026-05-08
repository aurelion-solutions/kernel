# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PolicyAssessmentDispatcher — structural placeholder for the future rule cartridge model.

Separates two concerns that will grow independently:
  1. Policy type selection  — which domain evaluator applies (SoD, lifecycle, etc.)
  2. Strategy selection     — how evidence is gathered (deterministic, semantic, …)

Current state:
  - Only AssessmentStrategy.DETERMINISTIC is wired; delegates to PolicyService.evaluate_policy().
  - All other strategies raise NotImplementedError as explicit placeholders.
  - No registry, no dynamic plugins, no DB-backed dispatch.

Do not add behaviour here; wire strategies explicitly when they are ready.
"""

from __future__ import annotations

from src.engines.policy_assessment.contracts import PolicyAssessmentOutput, PolicyAssessmentRequest
from src.engines.policy_assessment.schemas import AbstractState, Facts
from src.engines.policy_assessment.service import PolicyService
from src.engines.policy_assessment.strategies.deterministic.cartridge_evaluator import (
    evaluate_deterministic_cartridge,
)
from src.inventory.policy.enums import AssessmentStrategy, PolicyType

# Re-export so callers that already import from this module keep working.
__all__ = ['AssessmentStrategy', 'PolicyAssessmentDispatcher', 'PolicyType']


class PolicyAssessmentDispatcher:
    """Routes a PolicyAssessmentRequest to the appropriate strategy implementation.

    Both routing axes (policy_type, assessment_strategy) are carried by the request.
    Currently only AssessmentStrategy.DETERMINISTIC is wired.

    Args:
        policy_service: pre-built PolicyService instance (owns YAML rule loading).
    """

    def __init__(self, policy_service: PolicyService) -> None:
        self._policy_service = policy_service

    def evaluate(self, request: PolicyAssessmentRequest) -> PolicyAssessmentOutput:
        """Dispatch evaluation based on request.assessment_strategy.

        Returns PolicyAssessmentOutput where matched=True when the subject
        satisfies the policy (abstract_state==enabled for deterministic).

        Raises NotImplementedError for strategies not yet wired.
        """
        if request.assessment_strategy == AssessmentStrategy.DETERMINISTIC:
            return self._evaluate_deterministic(request)

        raise NotImplementedError(
            f'AssessmentStrategy {request.assessment_strategy!r} is not yet wired '
            'in PolicyAssessmentDispatcher. Add an explicit branch when ready.'
        )

    def _evaluate_deterministic(self, request: PolicyAssessmentRequest) -> PolicyAssessmentOutput:
        if 'condition' in request.policy_definition:
            return evaluate_deterministic_cartridge(request)
        facts = Facts.model_validate(request.context)
        decision = self._policy_service.evaluate_policy(facts)
        return PolicyAssessmentOutput(
            matched=decision.abstract_state == AbstractState.enabled,
            decision=decision,
        )
