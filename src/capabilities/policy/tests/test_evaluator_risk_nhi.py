# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — NHI-specific risk rules (Phase 6, Step 9)."""

from datetime import UTC, datetime

from src.capabilities.policy.evaluator import evaluate
from src.capabilities.policy.schemas import (
    AbstractState,
    Facts,
    OwnerFacts,
    RiskLevel,
    Rule,
    SubjectFacts,
    TargetFacts,
    ThreatFacts,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _owner(status: str = 'active') -> OwnerFacts:
    return OwnerFacts(id='owner-1', status=status)


def _nhi(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'nhi-1', 'kind': 'nhi', 'status': 'active', 'owner': _owner()}
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
    base: dict = {'subject': _nhi(), 'target': _target(), 'now': NOW}
    base.update(overrides)
    return Facts(**base)


# --- Rule definitions ---

RULE_NHI_LIFECYCLE_ACTIVE = Rule(
    id='nhi_active',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

RULE_NHI_OWNER_TERMINATED = Rule(
    id='nhi_owner_terminated',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.owner.status': 'terminated'},
    then={'abstract_state': 'disabled', 'signals': ['orphaned_nhi'], 'actions': ['disable_nhi']},
    precedence=110,
)

RULE_NHI_CREDENTIAL_EXPOSED = Rule(
    id='risk_nhi_credential_exposed',
    kind='risk',
    when={'subject.kind': 'nhi', 'threat.has_indicator': 'credential_exposed'},
    then={
        'abstract_state': 'disabled',
        'risk_level': 'critical',
        'actions': ['rotate_credential', 'revoke_all_tokens'],
        'signals': ['nhi_credential_incident'],
    },
    precedence=300,
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


# --- Tests ---


def test_nhi_credential_exposed_disables() -> None:
    """NHI active + credential_exposed indicator -> disabled, risk critical, risk overrides lifecycle."""
    facts = _facts(
        threat=_threat(active_indicators=['credential_exposed']),
    )
    decision = evaluate([RULE_NHI_LIFECYCLE_ACTIVE, RULE_NHI_CREDENTIAL_EXPOSED], facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.risk_level == RiskLevel.critical
    assert 'rotate_credential' in decision.actions
    assert 'revoke_all_tokens' in decision.actions
    assert 'nhi_credential_incident' in decision.signals


def test_nhi_credential_exposed_plus_owner_terminated() -> None:
    """NHI active + owner terminated + credential_exposed -> both rules fire, state disabled, actions accumulated."""
    facts = _facts(
        subject=_nhi(owner=_owner(status='terminated')),
        threat=_threat(active_indicators=['credential_exposed']),
    )
    decision = evaluate(
        [RULE_NHI_LIFECYCLE_ACTIVE, RULE_NHI_OWNER_TERMINATED, RULE_NHI_CREDENTIAL_EXPOSED],
        facts,
    )
    assert decision.abstract_state == AbstractState.disabled
    assert decision.risk_level == RiskLevel.critical
    # Actions from both rules accumulated
    assert 'rotate_credential' in decision.actions
    assert 'revoke_all_tokens' in decision.actions
    assert 'disable_nhi' in decision.actions
    # Signals from both rules accumulated
    assert 'nhi_credential_incident' in decision.signals
    assert 'orphaned_nhi' in decision.signals
    assert any(r.rule_id == 'risk_nhi_credential_exposed' for r in decision.reasons)
    assert any(r.rule_id == 'nhi_owner_terminated' for r in decision.reasons)


def test_nhi_dormant_suspends() -> None:
    """NHI active + days_since_last_login=150 (> 90) -> suspended (prec 200 > prec 10), risk medium."""
    facts = _facts(
        threat=_threat(days_since_last_login=150),
    )
    decision = evaluate([RULE_NHI_LIFECYCLE_ACTIVE, RULE_RISK_DORMANT], facts)
    assert decision.abstract_state == AbstractState.suspended
    assert decision.risk_level == RiskLevel.medium
    assert 'dormant_reactivation_review' in decision.signals


def test_nhi_no_threat_lifecycle_only() -> None:
    """NHI active + owner active + threat=None -> lifecycle determines decision, risk_level None."""
    facts = _facts(threat=None)
    decision = evaluate(
        [RULE_NHI_LIFECYCLE_ACTIVE, RULE_NHI_CREDENTIAL_EXPOSED, RULE_RISK_DORMANT],
        facts,
    )
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level is None
    assert any(r.rule_id == 'nhi_active' for r in decision.reasons)
    assert not any(r.rule_id in ('risk_nhi_credential_exposed', 'risk_dormant_reactivation') for r in decision.reasons)


def test_nhi_static_risk_admin_privilege() -> None:
    """NHI active + admin + production + no threat -> static risk_prod_admin matches even for NHI."""
    facts = _facts(
        target=_target(privilege_level='admin', environment='production'),
        threat=None,
    )
    decision = evaluate([RULE_NHI_LIFECYCLE_ACTIVE, RULE_RISK_PROD_ADMIN], facts)
    assert decision.risk_level == RiskLevel.high
    assert 'high_risk_access' in decision.signals
    assert any(r.rule_id == 'risk_prod_admin' for r in decision.reasons)
