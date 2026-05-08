# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — ITDR risk rules (Phase 6, Step 8)."""

from datetime import UTC, datetime

from src.engines.policy_assessment.schemas import (
    AbstractState,
    Facts,
    RiskLevel,
    Rule,
    SubjectFacts,
    TargetFacts,
    ThreatFacts,
)
from src.engines.policy_assessment.strategies.deterministic.evaluator import evaluate

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _employee(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'emp-1', 'kind': 'employee', 'status': 'active'}
    base.update(overrides)
    return SubjectFacts(**base)


def _nhi(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'nhi-1', 'kind': 'nhi', 'status': 'active'}
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

RULE_CREDENTIAL_COMPROMISED = Rule(
    id='risk_credential_compromised',
    kind='risk',
    when={'threat.has_indicator': 'credential_compromised'},
    then={
        'abstract_state': 'disabled',
        'risk_level': 'critical',
        'actions': ['force_password_reset', 'revoke_all_sessions'],
    },
    precedence=300,
)

RULE_IMPOSSIBLE_TRAVEL = Rule(
    id='risk_impossible_travel',
    kind='risk',
    when={'threat.has_indicator': 'impossible_travel'},
    then={
        'risk_level': 'high',
        'actions': ['revoke_session', 'require_step_up_mfa'],
        'signals': ['impossible_travel_review'],
    },
    precedence=250,
)

RULE_MFA_BOMBING = Rule(
    id='risk_mfa_bombing',
    kind='risk',
    when={'threat.has_indicator': 'mfa_bombing'},
    then={
        'risk_level': 'high',
        'actions': ['block_mfa_prompts', 'revoke_all_sessions'],
        'signals': ['mfa_bombing_incident'],
    },
    precedence=250,
)

RULE_BRUTE_FORCE = Rule(
    id='risk_brute_force',
    kind='risk',
    when={'threat.failed_auth_count': '> 10'},
    then={
        'risk_level': 'high',
        'actions': ['temporary_lockout', 'notify_security_team'],
    },
    precedence=240,
)

RULE_SESSION_HIJACK = Rule(
    id='risk_session_hijack',
    kind='risk',
    when={'threat.has_indicator': 'session_hijack'},
    then={
        'risk_level': 'critical',
        'actions': ['kill_session', 'force_reauth'],
        'signals': ['session_hijack_incident'],
    },
    precedence=290,
)

RULE_TOKEN_REPLAY = Rule(
    id='risk_token_replay',
    kind='risk',
    when={'threat.has_indicator': 'token_replay'},
    then={
        'risk_level': 'critical',
        'actions': ['revoke_token', 'force_reauth'],
        'signals': ['token_replay_incident'],
    },
    precedence=290,
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

ALL_ITDR_RULES = [
    RULE_CREDENTIAL_COMPROMISED,
    RULE_IMPOSSIBLE_TRAVEL,
    RULE_MFA_BOMBING,
    RULE_BRUTE_FORCE,
    RULE_SESSION_HIJACK,
    RULE_TOKEN_REPLAY,
    RULE_NHI_CREDENTIAL_EXPOSED,
]


def test_risk_credential_compromised_disables() -> None:
    """Employee + credential_compromised indicator -> disabled, risk_level critical."""
    facts = _facts(
        threat=_threat(active_indicators=['credential_compromised']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_CREDENTIAL_COMPROMISED], facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.risk_level == RiskLevel.critical
    assert 'force_password_reset' in decision.actions
    assert 'revoke_all_sessions' in decision.actions


def test_risk_impossible_travel_signals() -> None:
    """Employee + impossible_travel indicator -> risk_level high, signal, no abstract_state override."""
    facts = _facts(
        threat=_threat(active_indicators=['impossible_travel']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_IMPOSSIBLE_TRAVEL], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.high
    assert 'impossible_travel_review' in decision.signals
    assert 'revoke_session' in decision.actions
    assert 'require_step_up_mfa' in decision.actions


def test_risk_mfa_bombing_blocks() -> None:
    """Employee + mfa_bombing indicator -> risk_level high, signal mfa_bombing_incident."""
    facts = _facts(
        threat=_threat(active_indicators=['mfa_bombing']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_MFA_BOMBING], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.high
    assert 'mfa_bombing_incident' in decision.signals
    assert 'block_mfa_prompts' in decision.actions
    assert 'revoke_all_sessions' in decision.actions


def test_risk_brute_force_lockout() -> None:
    """Employee + failed_auth_count=15 (> 10) -> risk_level high, lockout actions."""
    facts = _facts(
        threat=_threat(failed_auth_count=15),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_BRUTE_FORCE], facts)
    assert decision.risk_level == RiskLevel.high
    assert 'temporary_lockout' in decision.actions
    assert 'notify_security_team' in decision.actions


def test_risk_brute_force_below_threshold_no_match() -> None:
    """Employee + failed_auth_count=5 (<= 10) -> brute_force rule does NOT match."""
    facts = _facts(
        threat=_threat(failed_auth_count=5),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_BRUTE_FORCE], facts)
    assert not any(r.rule_id == 'risk_brute_force' for r in decision.reasons)
    assert decision.risk_level is None


def test_risk_session_hijack_critical() -> None:
    """Employee + session_hijack indicator -> risk_level critical, signal, actions."""
    facts = _facts(
        threat=_threat(active_indicators=['session_hijack']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_SESSION_HIJACK], facts)
    assert decision.risk_level == RiskLevel.critical
    assert 'session_hijack_incident' in decision.signals
    assert 'kill_session' in decision.actions
    assert 'force_reauth' in decision.actions


def test_risk_token_replay_critical() -> None:
    """Employee + token_replay indicator -> risk_level critical, signal, actions."""
    facts = _facts(
        threat=_threat(active_indicators=['token_replay']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_TOKEN_REPLAY], facts)
    assert decision.risk_level == RiskLevel.critical
    assert 'token_replay_incident' in decision.signals
    assert 'revoke_token' in decision.actions
    assert 'force_reauth' in decision.actions


def test_risk_nhi_credential_exposed_disables() -> None:
    """NHI subject + credential_exposed indicator -> disabled, risk critical, signal."""
    facts = _facts(
        subject=_nhi(),
        threat=_threat(active_indicators=['credential_exposed']),
    )
    decision = evaluate([RULE_NHI_CREDENTIAL_EXPOSED], facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.risk_level == RiskLevel.critical
    assert 'rotate_credential' in decision.actions
    assert 'revoke_all_tokens' in decision.actions
    assert 'nhi_credential_incident' in decision.signals


def test_risk_no_threat_data_rules_skip() -> None:
    """Employee + facts.threat=None -> none of the ITDR risk rules match."""
    facts = _facts(threat=None)
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, *ALL_ITDR_RULES], facts)
    matched_ids = {r.rule_id for r in decision.reasons}
    itdr_ids = {r.id for r in ALL_ITDR_RULES}
    assert matched_ids.isdisjoint(itdr_ids)
    assert decision.risk_level is None


def test_risk_overrides_lifecycle_by_precedence() -> None:
    """Lifecycle -> enabled (prec 10), risk_credential_compromised -> disabled (prec 300). Risk wins."""
    facts = _facts(
        threat=_threat(active_indicators=['credential_compromised']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_CREDENTIAL_COMPROMISED], facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.risk_level == RiskLevel.critical


def test_risk_signals_accumulate_with_lifecycle() -> None:
    """Lifecycle -> enabled + impossible_travel (no abstract_state) -> state enabled, risk accumulated."""
    facts = _facts(
        threat=_threat(active_indicators=['impossible_travel']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_IMPOSSIBLE_TRAVEL], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.high
    assert 'impossible_travel_review' in decision.signals
    assert any(r.rule_id == 'employee_active' for r in decision.reasons)
    assert any(r.rule_id == 'risk_impossible_travel' for r in decision.reasons)


def test_risk_multiple_indicators_accumulate() -> None:
    """Employee + session_hijack + token_replay -> both rules match, actions/signals from both accumulated."""
    facts = _facts(
        threat=_threat(active_indicators=['session_hijack', 'token_replay']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_SESSION_HIJACK, RULE_TOKEN_REPLAY], facts)
    assert decision.risk_level == RiskLevel.critical
    assert 'session_hijack_incident' in decision.signals
    assert 'token_replay_incident' in decision.signals
    assert 'kill_session' in decision.actions
    assert 'force_reauth' in decision.actions
    assert 'revoke_token' in decision.actions


def test_risk_level_highest_precedence_wins() -> None:
    """Two risk rules: high (prec 250) + critical (prec 290) -> risk_level critical."""
    facts = _facts(
        threat=_threat(active_indicators=['impossible_travel', 'session_hijack']),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_IMPOSSIBLE_TRAVEL, RULE_SESSION_HIJACK], facts)
    assert decision.risk_level == RiskLevel.critical


def test_has_indicator_empty_list_no_match() -> None:
    """Employee + threat.active_indicators=[] -> has_indicator rule does NOT match."""
    facts = _facts(
        threat=_threat(active_indicators=[]),
    )
    decision = evaluate([RULE_LIFECYCLE_ACTIVE, RULE_CREDENTIAL_COMPROMISED], facts)
    assert not any(r.rule_id == 'risk_credential_compromised' for r in decision.reasons)
    assert decision.risk_level is None
