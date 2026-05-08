# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for PolicyAssessmentDispatcher."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput, PolicyAssessmentRequest
from src.engines.policy_assessment.dispatcher import AssessmentStrategy, PolicyAssessmentDispatcher, PolicyType
from src.engines.policy_assessment.schemas import (
    AbstractState,
    Decision,
    Facts,
    SubjectFacts,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

_ACTIVE_SUBJECT = SubjectFacts(id='s-1', kind='employee', status='active')
_FACTS = Facts(subject=_ACTIVE_SUBJECT, now=_NOW)
_FACTS_DICT = _FACTS.model_dump()


def _make_request(
    strategy: AssessmentStrategy = AssessmentStrategy.DETERMINISTIC,
    policy_type: PolicyType = PolicyType.LIFECYCLE,
) -> PolicyAssessmentRequest:
    return PolicyAssessmentRequest(
        policy_type=policy_type,
        assessment_strategy=strategy,
        context=_FACTS_DICT,
    )


def _make_dispatcher(abstract_state: AbstractState = AbstractState.enabled) -> PolicyAssessmentDispatcher:
    decision = Decision(abstract_state=abstract_state)
    policy_service = MagicMock()
    policy_service.evaluate_policy.return_value = decision
    return PolicyAssessmentDispatcher(policy_service=policy_service)


def test_deterministic_returns_policy_assessment_output() -> None:
    dispatcher = _make_dispatcher(AbstractState.enabled)
    result = dispatcher.evaluate(_make_request())
    assert isinstance(result, PolicyAssessmentOutput)


def test_deterministic_matched_true_when_enabled() -> None:
    dispatcher = _make_dispatcher(AbstractState.enabled)
    result = dispatcher.evaluate(_make_request())
    assert result.matched is True
    assert result.decision is not None
    assert result.decision.abstract_state == AbstractState.enabled


def test_deterministic_matched_false_when_suspended() -> None:
    dispatcher = _make_dispatcher(AbstractState.suspended)
    result = dispatcher.evaluate(_make_request())
    assert result.matched is False
    assert result.decision.abstract_state == AbstractState.suspended


def test_deterministic_matched_false_when_disabled() -> None:
    dispatcher = _make_dispatcher(AbstractState.disabled)
    result = dispatcher.evaluate(_make_request())
    assert result.matched is False


def test_unsupported_strategy_raises_not_implemented() -> None:
    dispatcher = _make_dispatcher()
    bad_request = PolicyAssessmentRequest(
        policy_type=PolicyType.LIFECYCLE,
        assessment_strategy=AssessmentStrategy.SEMANTIC_ASSISTED,
        context=_FACTS_DICT,
    )
    with pytest.raises(NotImplementedError, match='not yet wired'):
        dispatcher.evaluate(bad_request)


def test_delegates_to_policy_service() -> None:
    decision = Decision(abstract_state=AbstractState.enabled)
    policy_service = MagicMock()
    policy_service.evaluate_policy.return_value = decision
    dispatcher = PolicyAssessmentDispatcher(policy_service=policy_service)

    dispatcher.evaluate(_make_request())

    policy_service.evaluate_policy.assert_called_once_with(_FACTS)


# ---------------------------------------------------------------------------
# PolicyType enum values
# ---------------------------------------------------------------------------


def test_policy_type_values() -> None:
    assert PolicyType.SOD == 'sod'
    assert PolicyType.ACCESS_RISK == 'access_risk'
    assert PolicyType.LIFECYCLE == 'lifecycle'
    assert PolicyType.NHI == 'nhi'
    assert PolicyType.PRIVILEGED_ACCESS == 'privileged_access'


def test_policy_type_members() -> None:
    members = {pt.value for pt in PolicyType}
    assert members == {'sod', 'access_risk', 'lifecycle', 'nhi', 'privileged_access'}
