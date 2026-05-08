# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for SemanticPolicyAssessmentService placeholder."""

from __future__ import annotations

import pytest
from src.engines.policy_assessment.strategies.semantic_assisted.schemas import SemanticPolicyAssessmentRequest
from src.engines.policy_assessment.strategies.semantic_assisted.service import SemanticPolicyAssessmentService


@pytest.mark.asyncio
async def test_assess_returns_not_matched() -> None:
    svc = SemanticPolicyAssessmentService()
    result = await svc.assess(SemanticPolicyAssessmentRequest(policy_id='test-policy'))
    assert result.matched is False


@pytest.mark.asyncio
async def test_assess_returns_empty_evidence_and_signals() -> None:
    svc = SemanticPolicyAssessmentService()
    result = await svc.assess(SemanticPolicyAssessmentRequest(policy_id='p', context={'k': 'v'}))
    assert result.evidence == []
    assert result.signals == []
    assert result.confidence is None
    assert result.explanation is None


@pytest.mark.asyncio
async def test_assess_is_deterministic() -> None:
    svc = SemanticPolicyAssessmentService()
    req = SemanticPolicyAssessmentRequest(policy_id='same-policy')
    r1 = await svc.assess(req)
    r2 = await svc.assess(req)
    assert r1 == r2
