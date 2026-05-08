# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Schemas for semantic-assisted policy assessment.

SemanticPolicyAssessmentResult is support material only — not a final decision authority.
The caller (policy evaluator) decides how to weight it against rule-based signals.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SemanticPolicyAssessmentRequest(BaseModel):
    """Input to semantic-assisted assessment.

    policy_id: stable identifier for the policy being evaluated (slug or UUID str).
    context: arbitrary key/value facts about the subject/resource being assessed.
    """

    policy_id: str
    context: dict[str, Any] = {}


class SemanticPolicyAssessmentEvidence(BaseModel):
    """One piece of semantic evidence returned by the assessment."""

    source: str
    excerpt: str


class SemanticPolicyAssessmentResult(BaseModel):
    """Support material produced by semantic-assisted assessment.

    Not a final decision — the policy evaluator decides how to incorporate this.

    matched: whether the semantic assessment considers the policy condition satisfied.
    confidence: optional float in [0, 1]; None when the strategy cannot estimate.
    evidence: verbatim source excerpts that informed the assessment.
    explanation: optional human-readable summary of the reasoning.
    signals: short string tags extracted from the assessment (e.g. 'inactive_90d').
    """

    matched: bool
    confidence: float | None = None
    evidence: list[SemanticPolicyAssessmentEvidence] = []
    explanation: str | None = None
    signals: list[str] = []
