# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for policy assessment contracts — construction and defaults."""

from __future__ import annotations

from src.engines.policy_assessment.contracts import (
    PolicyAssessmentEvidence,
    PolicyAssessmentOutput,
    PolicyAssessmentRequest,
    PolicyAssessmentSignal,
)
from src.engines.policy_assessment.schemas import AbstractState, Decision
from src.inventory.policy.enums import AssessmentStrategy, PolicyType


def test_output_defaults() -> None:
    out = PolicyAssessmentOutput(matched=False)
    assert out.matched is False
    assert out.decision is None
    assert out.signals == []
    assert out.evidence == []
    assert out.explanation is None
    assert out.confidence is None
    assert out.payload == {}


def test_output_with_decision() -> None:
    decision = Decision(abstract_state=AbstractState.enabled)
    out = PolicyAssessmentOutput(matched=True, decision=decision)
    assert out.matched is True
    assert out.decision.abstract_state == AbstractState.enabled


def test_signal_defaults() -> None:
    sig = PolicyAssessmentSignal(code='inactive_90d')
    assert sig.severity is None
    assert sig.message is None
    assert sig.payload == {}


def test_evidence_defaults() -> None:
    ev = PolicyAssessmentEvidence(source_type='yaml_rule')
    assert ev.source_id is None
    assert ev.title is None
    assert ev.summary is None
    assert ev.payload == {}


def test_output_with_signals_and_evidence() -> None:
    sig = PolicyAssessmentSignal(code='high_risk', severity='high', message='user inactive')
    ev = PolicyAssessmentEvidence(source_type='log', source_id='evt-1', summary='last login 120d ago')
    out = PolicyAssessmentOutput(matched=True, signals=[sig], evidence=[ev], confidence=0.9)
    assert len(out.signals) == 1
    assert out.signals[0].code == 'high_risk'
    assert len(out.evidence) == 1
    assert out.evidence[0].source_id == 'evt-1'
    assert out.confidence == 0.9


# ---------------------------------------------------------------------------
# PolicyAssessmentRequest
# ---------------------------------------------------------------------------


def test_request_required_fields() -> None:
    req = PolicyAssessmentRequest(
        policy_type=PolicyType.SOD,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
    )
    assert req.policy_type == PolicyType.SOD
    assert req.assessment_strategy == AssessmentStrategy.DETERMINISTIC


def test_request_defaults() -> None:
    req = PolicyAssessmentRequest(
        policy_type=PolicyType.LIFECYCLE,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
    )
    assert req.policy_id is None
    assert req.policy_definition == {}
    assert req.context == {}
    assert req.payload == {}


def test_request_with_all_fields() -> None:
    req = PolicyAssessmentRequest(
        policy_type=PolicyType.ACCESS_RISK,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
        policy_id='risk-001',
        policy_definition={'rules': []},
        context={'subject_id': 's-1', 'status': 'active'},
        payload={'trace_id': 'abc'},
    )
    assert req.policy_id == 'risk-001'
    assert req.policy_definition == {'rules': []}
    assert req.context['subject_id'] == 's-1'
    assert req.payload['trace_id'] == 'abc'
