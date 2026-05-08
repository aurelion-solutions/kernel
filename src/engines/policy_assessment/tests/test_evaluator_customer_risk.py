# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — Customer (CIAM) risk rules (Phase 6, Step 10)."""

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


def _subject(**overrides: object) -> SubjectFacts:
    base: dict = {
        'id': 'cust-1',
        'kind': 'customer',
        'status': 'active',
        'email_verified': True,
        'required_consents_met': True,
        'mfa_enabled': True,
        'plan_tier': 'pro',
    }
    base.update(overrides)
    return SubjectFacts(**base)


def _target(**overrides: object) -> TargetFacts:
    base: dict = {'application': 'customer_portal'}
    base.update(overrides)
    return TargetFacts(**base)


def _threat(**overrides: object) -> ThreatFacts:
    return ThreatFacts(**overrides)


def _facts(**overrides: object) -> Facts:
    base: dict = {'subject': _subject(), 'target': _target(), 'now': NOW}
    base.update(overrides)
    return Facts(**base)


# --- Rule definitions mirroring risk.yaml CIAM subset ---

RULE_CUST_ACTIVE = Rule(
    id='cust_active',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

RULE_CUST_BANNED = Rule(
    id='cust_banned',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'banned'},
    then={
        'abstract_state': 'disabled',
        'actions': ['revoke_all_sessions', 'purge_api_keys'],
    },
    precedence=100,
)

RULE_EMPLOYEE_ACTIVE = Rule(
    id='employee_active',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

RULE_ATO = Rule(
    id='risk_account_takeover',
    kind='risk',
    when={
        'subject.kind': 'customer',
        'threat.has_indicator': 'account_takeover',
    },
    then={
        'abstract_state': 'disabled',
        'risk_level': 'critical',
        'actions': ['revoke_all_sessions', 'force_password_reset', 'notify_customer_email'],
    },
    precedence=300,
)

RULE_CREDENTIAL_STUFFING = Rule(
    id='risk_credential_stuffing',
    kind='risk',
    when={
        'subject.kind': 'customer',
        'threat.has_indicator': 'credential_stuffing',
    },
    then={
        'risk_level': 'high',
        'actions': ['require_captcha', 'rate_limit_auth'],
        'signals': ['credential_stuffing_detected'],
    },
    precedence=260,
)

RULE_BOT_DETECTED = Rule(
    id='risk_bot_detected',
    kind='risk',
    when={
        'subject.kind': 'customer',
        'threat.has_indicator': 'bot_detected',
    },
    then={
        'risk_level': 'high',
        'actions': ['block_request', 'require_captcha'],
    },
    precedence=250,
)

RULE_DEVICE_ANOMALY = Rule(
    id='risk_device_anomaly',
    kind='risk',
    when={
        'subject.kind': 'customer',
        'threat.has_indicator': 'device_anomaly',
    },
    then={
        'risk_level': 'medium',
        'actions': ['require_step_up_mfa'],
        'signals': ['device_anomaly_review'],
    },
    precedence=200,
)

RULE_ENTERPRISE_NO_MFA = Rule(
    id='risk_customer_no_mfa_enterprise',
    kind='risk',
    when={
        'subject.kind': 'customer',
        'subject.plan_tier': 'enterprise',
        'subject.mfa_enabled': False,
    },
    then={
        'risk_level': 'medium',
        'signals': ['recommend_mfa_enrollment'],
    },
    precedence=130,
)


# --- Tests ---


def test_ciam_risk_ato_disables() -> None:
    """Customer active + account_takeover indicator -> disabled (prec 300 > 10), risk critical."""
    facts = _facts(threat=_threat(active_indicators=['account_takeover']))
    decision = evaluate([RULE_CUST_ACTIVE, RULE_ATO], facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.risk_level == RiskLevel.critical
    assert 'revoke_all_sessions' in decision.actions
    assert 'force_password_reset' in decision.actions
    assert 'notify_customer_email' in decision.actions
    assert any(r.rule_id == 'risk_account_takeover' for r in decision.reasons)


def test_ciam_risk_ato_kind_guard() -> None:
    """Employee subject + account_takeover indicator -> ATO rule does NOT match (kind != customer)."""
    facts = _facts(
        subject=SubjectFacts(id='emp-1', kind='employee', status='active'),
        threat=_threat(active_indicators=['account_takeover']),
    )
    decision = evaluate([RULE_EMPLOYEE_ACTIVE, RULE_ATO], facts)
    assert not any(r.rule_id == 'risk_account_takeover' for r in decision.reasons)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level is None


def test_ciam_risk_credential_stuffing() -> None:
    """Customer active + credential_stuffing -> state enabled (no abstract_state on risk), risk high."""
    facts = _facts(threat=_threat(active_indicators=['credential_stuffing']))
    decision = evaluate([RULE_CUST_ACTIVE, RULE_CREDENTIAL_STUFFING], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.high
    assert 'require_captcha' in decision.actions
    assert 'rate_limit_auth' in decision.actions
    assert 'credential_stuffing_detected' in decision.signals
    assert any(r.rule_id == 'risk_credential_stuffing' for r in decision.reasons)


def test_ciam_risk_bot_detected() -> None:
    """Customer active + bot_detected -> state enabled, risk high, block actions."""
    facts = _facts(threat=_threat(active_indicators=['bot_detected']))
    decision = evaluate([RULE_CUST_ACTIVE, RULE_BOT_DETECTED], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.high
    assert 'block_request' in decision.actions
    assert 'require_captcha' in decision.actions
    assert any(r.rule_id == 'risk_bot_detected' for r in decision.reasons)


def test_ciam_risk_device_anomaly() -> None:
    """Customer active + device_anomaly -> state enabled, risk medium, step-up mfa action."""
    facts = _facts(threat=_threat(active_indicators=['device_anomaly']))
    decision = evaluate([RULE_CUST_ACTIVE, RULE_DEVICE_ANOMALY], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.medium
    assert 'require_step_up_mfa' in decision.actions
    assert 'device_anomaly_review' in decision.signals
    assert any(r.rule_id == 'risk_device_anomaly' for r in decision.reasons)


def test_ciam_risk_enterprise_no_mfa_static() -> None:
    """Customer enterprise + mfa_enabled=False + threat=None -> state enabled, risk medium, signal."""
    facts = _facts(
        subject=_subject(plan_tier='enterprise', mfa_enabled=False),
        threat=None,
    )
    decision = evaluate([RULE_CUST_ACTIVE, RULE_ENTERPRISE_NO_MFA], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level == RiskLevel.medium
    assert 'recommend_mfa_enrollment' in decision.signals
    assert any(r.rule_id == 'risk_customer_no_mfa_enterprise' for r in decision.reasons)


def test_ciam_risk_enterprise_mfa_enabled_no_match() -> None:
    """Customer enterprise + mfa_enabled=True -> enterprise_no_mfa rule does NOT match."""
    facts = _facts(
        subject=_subject(plan_tier='enterprise', mfa_enabled=True),
        threat=None,
    )
    decision = evaluate([RULE_CUST_ACTIVE, RULE_ENTERPRISE_NO_MFA], facts)
    assert not any(r.rule_id == 'risk_customer_no_mfa_enterprise' for r in decision.reasons)
    assert decision.risk_level is None
    assert decision.abstract_state == AbstractState.enabled


def test_ciam_risk_enterprise_no_mfa_non_enterprise_no_match() -> None:
    """Customer pro + mfa_enabled=False -> enterprise_no_mfa rule does NOT match (plan_tier != enterprise)."""
    facts = _facts(
        subject=_subject(plan_tier='pro', mfa_enabled=False),
        threat=None,
    )
    decision = evaluate([RULE_CUST_ACTIVE, RULE_ENTERPRISE_NO_MFA], facts)
    assert not any(r.rule_id == 'risk_customer_no_mfa_enterprise' for r in decision.reasons)
    assert decision.risk_level is None
    assert decision.abstract_state == AbstractState.enabled


def test_ciam_risk_ato_overrides_banned_lifecycle() -> None:
    """Customer banned + account_takeover -> both set disabled, no conflict, actions accumulated."""
    facts = _facts(
        subject=_subject(status='banned'),
        threat=_threat(active_indicators=['account_takeover']),
    )
    decision = evaluate([RULE_CUST_BANNED, RULE_ATO], facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.risk_level == RiskLevel.critical
    assert 'revoke_all_sessions' in decision.actions
    assert 'force_password_reset' in decision.actions
    assert 'notify_customer_email' in decision.actions
    assert 'purge_api_keys' in decision.actions
    assert any(r.rule_id == 'risk_account_takeover' for r in decision.reasons)
    assert any(r.rule_id == 'cust_banned' for r in decision.reasons)


def test_ciam_risk_precedence_ato_vs_credential_stuffing() -> None:
    """Customer + both ATO (prec 300) and credential_stuffing (prec 260) -> risk critical (ATO wins)."""
    facts = _facts(
        threat=_threat(active_indicators=['account_takeover', 'credential_stuffing']),
    )
    decision = evaluate([RULE_CUST_ACTIVE, RULE_ATO, RULE_CREDENTIAL_STUFFING], facts)
    assert decision.risk_level == RiskLevel.critical
    assert decision.abstract_state == AbstractState.disabled
    assert 'revoke_all_sessions' in decision.actions
    assert 'force_password_reset' in decision.actions
    assert 'notify_customer_email' in decision.actions
    assert 'require_captcha' in decision.actions
    assert 'rate_limit_auth' in decision.actions
    assert 'credential_stuffing_detected' in decision.signals
    assert any(r.rule_id == 'risk_account_takeover' for r in decision.reasons)
    assert any(r.rule_id == 'risk_credential_stuffing' for r in decision.reasons)
    assert any(r.rule_id == 'cust_active' for r in decision.reasons)


def test_ciam_risk_no_threat_no_enterprise_lifecycle_only() -> None:
    """Customer active + threat=None + plan_tier=pro -> no risk rules match, lifecycle only."""
    facts = _facts(
        subject=_subject(plan_tier='pro'),
        threat=None,
    )
    all_rules = [
        RULE_CUST_ACTIVE,
        RULE_ATO,
        RULE_CREDENTIAL_STUFFING,
        RULE_BOT_DETECTED,
        RULE_DEVICE_ANOMALY,
        RULE_ENTERPRISE_NO_MFA,
    ]
    decision = evaluate(all_rules, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.risk_level is None
    assert any(r.rule_id == 'cust_active' for r in decision.reasons)
    risk_ids = {
        'risk_account_takeover',
        'risk_credential_stuffing',
        'risk_bot_detected',
        'risk_device_anomaly',
        'risk_customer_no_mfa_enterprise',
    }
    matched_ids = {r.rule_id for r in decision.reasons}
    assert matched_ids.isdisjoint(risk_ids)


def test_ciam_risk_device_anomaly_plus_enterprise_no_mfa() -> None:
    """Customer enterprise + mfa_enabled=False + device_anomaly -> both risk rules match."""
    facts = _facts(
        subject=_subject(plan_tier='enterprise', mfa_enabled=False),
        threat=_threat(active_indicators=['device_anomaly']),
    )
    decision = evaluate([RULE_CUST_ACTIVE, RULE_DEVICE_ANOMALY, RULE_ENTERPRISE_NO_MFA], facts)
    # device_anomaly (prec 200) wins over enterprise_no_mfa (prec 130), both are medium anyway
    assert decision.risk_level == RiskLevel.medium
    assert decision.abstract_state == AbstractState.enabled
    assert 'require_step_up_mfa' in decision.actions
    assert 'device_anomaly_review' in decision.signals
    assert 'recommend_mfa_enrollment' in decision.signals
    assert any(r.rule_id == 'risk_device_anomaly' for r in decision.reasons)
    assert any(r.rule_id == 'risk_customer_no_mfa_enterprise' for r in decision.reasons)
