# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Semantic-assisted policy assessment service.

Owns how semantic evidence is used during policy evaluation.
Does NOT own semantic providers, clients, embeddings, or RAG infrastructure —
those live in platform/llm.

Current implementation: deterministic placeholder (no real provider calls).
"""

from __future__ import annotations

from src.engines.policy_assessment.strategies.semantic_assisted.schemas import (
    SemanticPolicyAssessmentRequest,
    SemanticPolicyAssessmentResult,
)


class SemanticPolicyAssessmentService:
    """Evaluates a policy assessment request using semantic evidence.

    Placeholder implementation — returns matched=False with no evidence or signals.
    Real semantic provider integration is a future phase concern.
    """

    async def assess(self, request: SemanticPolicyAssessmentRequest) -> SemanticPolicyAssessmentResult:
        return SemanticPolicyAssessmentResult(
            matched=False,
            confidence=None,
            evidence=[],
            explanation=None,
            signals=[],
        )
