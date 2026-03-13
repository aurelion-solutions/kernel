# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — static risk rules and UEBA score rules (Phase 6, Step 9)."""

from datetime import UTC, datetime

from src.capabilities.policy.evaluator import evaluate
from src.capabilities.policy.schemas import (
    AbstractState,
    Facts,
    RiskLevel,
    Rule,
    SubjectFacts,
    TargetFacts,
    ThreatFacts,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _employee(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'emp-1', 'kind': 'employee', 'status': 'active'}
    base.update(overrides)
    return SubjectFacts(**base)


def _target(**overrides: object) -> TargetFacts:
    base: dict = {'application': 'app-1'}
    base.update(overrides)
    return TargetFacts(**base)


def _threat(**overrides: object) -> ThreatFacts:
    base: dict = {}
    base.update(overrides)
    return ThreatFacts(**base)


def _facts(**overrides: object) -> Facts:
    base: dict = {'subject': _employee(), 'target': _target(), 'now': NOW}
    base.update(overrides)
    return Facts(**base)


# --- Rule definitions ---

RULE_LIFECYCLE_ACTIVE = Rule(
    id='employee_active',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

RULE_RISK_ADMIN_NO_MFA = Rule(
    id='risk_admin_no_mfa',
    kind='risk',
    when={'target.privilege_level': 'admin', 'subject.mfa_enabled': False},
    then={
        'risk_level': 'critical',
        'actions': ['require_mfa_enrollment'],
        'signals': ['admin_without_mfa'],
    },
    precedence=220,
)

RULE_RISK_PROD_ADMIN = Rule(
    id='risk_prod_admin',
    kind='risk',
    when={'target.privilege_level': 'admin', 'target.environment': 'production'},
    then={
        'risk_level': 'high',
        'signals': ['high_risk_access'],
    },
    precedence=150,
)

RULE_RISK_PII_ACCESS = Rule(
    id='risk_pii_access',
    kind='risk',
    when={'target.data_sensitivity': 'pii'},
    then={
        'risk_level': 'medium',
        'signals': ['pii_access_logged'],
    },
    precedence=100,
)

RULE_RISK_DORMANT = Rule(
    id='risk_dormant_reactivation',
    kind='risk',
    when={'threat.days_since_last_login': '> 90'},
    then={
        'risk_level': 'medium',
        'abstract_state': 'suspended',
        'signals': ['dormant_reactivation_review'],
    },
    precedence=200,
)

RULE_RISK_SCORE_CRITICAL = Rule(
    id='risk_score_critical',
    kind='risk',
    when={'threat.risk_score': '> 0.9'},
    then={
        'risk_level': 'critical',
        'abstract_state': 'suspended',
        'actions': ['revoke_all_sessions'],
        'signals': ['ueba_critical'],
    },
    precedence=280,
)

RULE_RISK_SCORE_HIGH = Rule(
    id='risk_score_high',
    kind='risk',
    when={'threat.risk_score': '0.7..0.9'},
    then={
        'risk_level': 'high',
        'signals': ['ueba_high_review'],
    },
    precedence=180,
)


# --- Tests ---


def test_risk_admin_no_mfa_critical() -> None:
    """Employee active + admin privilege + mfa_enabled=False -> risk critical, no state override."""
    facts = _facts(
        subject=_employee(mfa_enabled=False),
        target=_target(privilege_level='admin'),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_ADMIN_NO_MFA], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.critical
    assert 'require_mfa_enrollment' in decision.actions
    assert 'admin_without_mfa' in decision.signals


def test_risk_admin_with_mfa_no_match() -> None:
    """Employee active + admin privilege + mfa_enabled=True -> risk_admin_no_mfa does NOT match."""
    facts = _facts(
        subject=_employee(mfa_enabled=True),
        target=_target(privilege_level='admin'),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_ADMIN_NO_MFA], facts)
    assert not any(r.rule_id == 'risk_admin_no_mfa' for r in decision.reasons)
    assert decision.risk_level is None


def test_risk_prod_admin_high() -> None:
    """Employee active + admin + production -> risk high, signal high_risk_access, no state override."""
    facts = _facts(
        target=_target(privilege_level='admin', environment='production'),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_PROD_ADMIN], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.high
    assert 'high_risk_access' in decision.signals


def test_risk_pii_access_medium() -> None:
    """Employee active + data_sensitivity=pii -> risk medium, signal pii_access_logged, no state override."""
    facts = _facts(
        target=_target(data_sensitivity='pii'),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_PII_ACCESS], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.medium
    assert 'pii_access_logged' in decision.signals


def test_risk_admin_no_mfa_and_prod_admin_both_match() -> None:
    """Both risk_admin_no_mfa (prec 220) and risk_prod_admin (prec 150) match.

    risk_level: critical (higher precedence wins), signals from both accumulated.
    """
    facts = _facts(
        subject=_employee(mfa_enabled=False),
        target=_target(privilege_level='admin', environment='production'),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_ADMIN_NO_MFA, RULE_RISK_PROD_ADMIN], facts)
    assert decision.risk_level == RiskLevel.critical
    assert 'admin_without_mfa' in decision.signals
    assert 'high_risk_access' in decision.signals
    assert 'require_mfa_enrollment' in decision.actions


def test_risk_dormant_suspends() -> None:
    """Employee active + days_since_last_login=120 (> 90) -> suspended (prec 200 > prec 10), risk medium."""
    facts = _facts(
        threat=_threat(days_since_last_login=120),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_DORMANT], facts)
    assert decision.abstract_state == AbstractState.suspended
    assert decision.risk_level == RiskLevel.medium
    assert 'dormant_reactivation_review' in decision.signals


def test_risk_dormant_below_threshold_no_match() -> None:
    """Employee active + days_since_last_login=30 (<= 90) -> dormant rule does NOT match."""
    facts = _facts(
        threat=_threat(days_since_last_login=30),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_DORMANT], facts)
    assert not any(r.rule_id == 'risk_dormant_reactivation' for r in decision.reasons)
    assert decision.risk_level is None


def test_risk_score_critical_suspends() -> None:
    """Employee active + risk_score=0.95 (> 0.9) -> suspended (prec 280), risk critical, actions."""
    facts = _facts(
        threat=_threat(risk_score=0.95),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_SCORE_CRITICAL], facts)
    assert decision.abstract_state == AbstractState.suspended
    assert decision.risk_level == RiskLevel.critical
    assert 'revoke_all_sessions' in decision.actions
    assert 'ueba_critical' in decision.signals


def test_risk_score_high_range() -> None:
    """Employee active + risk_score=0.8 (in 0.7..0.9) -> risk high, signal ueba_high_review, no state override."""
    facts = _facts(
        threat=_threat(risk_score=0.8),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_SCORE_HIGH], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.high
    assert 'ueba_high_review' in decision.signals


def test_risk_score_below_range_no_match() -> None:
    """Employee active + risk_score=0.5 (below 0.7) -> neither risk_score_high nor risk_score_critical match."""
    facts = _facts(
        threat=_threat(risk_score=0.5),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_SCORE_HIGH, RULE_RISK_SCORE_CRITICAL], facts)
    assert not any(r.rule_id in ('risk_score_high', 'risk_score_critical') for r in decision.reasons)
    assert decision.risk_level is None


def test_risk_score_at_range_boundary_exclusive() -> None:
    """Employee active + risk_score=0.9 (right-exclusive boundary) -> risk_score_high does NOT match.

    risk_score_critical also does NOT match (not strictly > 0.9).
    """
    facts = _facts(
        threat=_threat(risk_score=0.9),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_SCORE_HIGH, RULE_RISK_SCORE_CRITICAL], facts)
    assert not any(r.rule_id in ('risk_score_high', 'risk_score_critical') for r in decision.reasons)
    assert decision.risk_level is None


def test_risk_no_threat_static_rules_still_work() -> None:
    """Employee active + threat=None + admin + mfa_enabled=False -> static risk still matches."""
    facts = _facts(
        subject=_employee(mfa_enabled=False),
        target=_target(privilege_level='admin'),
        threat=None,
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_ADMIN_NO_MFA], facts)
    assert decision.risk_level == RiskLevel.critical
    assert 'admin_without_mfa' in decision.signals


def test_risk_without_threat_lifecycle_plus_static() -> None:
    """Employee active + threat=None + no admin/pii conditions -> no risk rules match, risk_level None."""
    facts = _facts(
        threat=None,
    )
    decision = evaluate(
        [RULE_LIFECYCLE_ACTIVE, RULE_RISK_ADMIN_NO_MFA, RULE_RISK_PII_ACCESS, RULE_RISK_DORMANT],
        facts,
    )
    risk_ids = {'risk_admin_no_mfa', 'risk_pii_access', 'risk_dormant_reactivation'}
    assert all(r.rule_id not in risk_ids for r in decision.reasons)
    assert decision.risk_level is None


def test_numeric_range_left_inclusive() -> None:
    """Employee active + risk_score=0.7 (left-inclusive boundary) -> risk_score_high MATCHES."""
    facts = _facts(
        threat=_threat(risk_score=0.7),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_RISK_SCORE_HIGH], facts)
    assert any(r.rule_id == 'risk_score_high' for r in decision.reasons)
    assert decision.risk_level == RiskLevel.high
