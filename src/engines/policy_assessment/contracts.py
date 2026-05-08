# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared internal output contracts for policy assessment.

These models provide a common shape that deterministic, semantic-assisted,
and policy-type adapters can eventually normalize into.
Not persisted; not exposed via API.

Usage: strategy adapters convert their native outputs into PolicyAssessmentOutput.
The policy engine then uses PolicyAssessmentOutput for aggregation and ranking.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from src.engines.policy_assessment.schemas import Decision
from src.inventory.policy.enums import AssessmentStrategy, PolicyType


class PolicyAssessmentRequest(BaseModel):
    """Explicit request model that carries both routing axes.

    policy_type: which policy domain is being evaluated (SoD, risk, lifecycle, …).
    assessment_strategy: how evidence is gathered (deterministic, semantic_assisted, …).
    policy_id: stable identifier for the specific policy, when applicable.
    policy_definition: inline policy definition, e.g. a rule-pack dict.
    context: arbitrary key/value facts about the subject/resource being assessed.
    payload: strategy-specific extras for debugging/tracing.
    """

    policy_type: PolicyType
    assessment_strategy: AssessmentStrategy
    policy_id: str | None = None
    policy_definition: dict[str, Any] = {}
    context: dict[str, Any] = {}
    payload: dict[str, Any] = {}


class PolicyAssessmentEvidence(BaseModel):
    """One piece of evidence that informed the assessment."""

    source_type: str
    source_id: str | None = None
    title: str | None = None
    summary: str | None = None
    payload: dict[str, Any] = {}


class PolicyAssessmentSignal(BaseModel):
    """A discrete signal extracted from assessment output."""

    code: str
    severity: str | None = None
    message: str | None = None
    payload: dict[str, Any] = {}


class PolicyAssessmentOutput(BaseModel):
    """Normalized output from any policy assessment strategy.

    matched: whether the assessed policy condition is considered satisfied.
    decision: structured Decision when the strategy produces one (YAML evaluator).
    signals: discrete signals extracted from the assessment.
    evidence: source excerpts or data points that informed the result.
    explanation: optional human-readable summary.
    confidence: optional float in [0, 1]; None when the strategy cannot estimate.
    payload: arbitrary strategy-specific extras for debugging/tracing.
    """

    matched: bool
    decision: Decision | None = None
    signals: list[PolicyAssessmentSignal] = []
    evidence: list[PolicyAssessmentEvidence] = []
    explanation: str | None = None
    confidence: float | None = None
    payload: dict[str, Any] = {}
