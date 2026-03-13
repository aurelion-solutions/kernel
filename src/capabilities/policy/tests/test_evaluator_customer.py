# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — Customer (CIAM) lifecycle rules (Phase 6, Step 6)."""

from datetime import UTC, datetime, timedelta

from src.capabilities.policy.evaluator import evaluate
from src.capabilities.policy.schemas import (
    AbstractState,
    Facts,
    Initiative,
    Rule,
    SubjectFacts,
    TargetFacts,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _subject(**overrides: object) -> SubjectFacts:
    base: dict = {
        'id': 'cust-1',
        'kind': 'customer',
        'status': 'active',
        'email_verified': True,
        'required_consents_met': True,
    }
    base.update(overrides)
    return SubjectFacts(**base)


def _target(**overrides: object) -> TargetFacts:
    base: dict = {'application': 'app-1'}
    base.update(overrides)
    return TargetFacts(**base)


def _facts(**overrides: object) -> Facts:
    base: dict = {'subject': _subject(), 'target': _target(), 'now': NOW}
    base.update(overrides)
    return Facts(**base)


def _trial_initiative(valid_until: datetime) -> Initiative:
    return Initiative(type='trial', valid_until=valid_until)


# --- Rule definitions mirroring lifecycle.yaml customer subset ---

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

RULE_CUST_DELETION_REQUESTED = Rule(
    id='cust_deletion_requested',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'deletion_requested'},
    then={
        'abstract_state': 'grace',
        'actions': ['revoke_new_sessions', 'schedule_data_deletion'],
        'signals': ['gdpr_deletion_pending'],
    },
    precedence=95,
)

RULE_CUST_TENANT_SUSPENDED = Rule(
    id='cust_tenant_suspended',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.tenant_status': 'suspended'},
    then={
        'abstract_state': 'suspended',
        'signals': ['tenant_suspension'],
    },
    precedence=90,
)

RULE_CUST_TRIAL_EXPIRED = Rule(
    id='cust_trial_expired',
    kind='lifecycle',
    when={
        'subject.kind': 'customer',
        'target.has_initiative': 'trial',
        'target.initiative.trial.valid_until': '<= now',
    },
    then={
        'abstract_state': 'disabled',
        'actions': ['show_upgrade_prompt'],
        'signals': ['trial_expired'],
    },
    precedence=85,
)

RULE_CUST_SUSPENDED = Rule(
    id='cust_suspended',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'suspended'},
    then={
        'abstract_state': 'suspended',
        'actions': ['restrict_to_readonly'],
    },
    precedence=80,
)

RULE_CUST_CONSENT_WITHDRAWN = Rule(
    id='cust_consent_withdrawn',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.required_consents_met': False},
    then={
        'abstract_state': 'disabled',
        'signals': ['consent_violation'],
    },
    precedence=75,
)

RULE_CUST_NOT_VERIFIED = Rule(
    id='cust_not_verified',
    kind='lifecycle',
    when={
        'subject.kind': 'customer',
        'subject.status': 'registered',
        'subject.email_verified': False,
    },
    then={
        'abstract_state': 'pending',
        'actions': ['send_verification_email'],
    },
    precedence=50,
)

RULE_CUST_VERIFIED = Rule(
    id='cust_verified',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'verified'},
    then={'abstract_state': 'enabled'},
    precedence=40,
)

RULE_CUST_TRIAL_EXPIRING = Rule(
    id='cust_trial_expiring',
    kind='lifecycle',
    when={
        'subject.kind': 'customer',
        'target.has_initiative': 'trial',
        'target.initiative.trial.valid_until': 'now..now+7d',
    },
    then={'signals': ['trial_expiring_soon']},
    precedence=30,
)

RULE_CUST_ACTIVE = Rule(
    id='cust_active',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

ALL_RULES = [
    RULE_CUST_BANNED,
    RULE_CUST_DELETION_REQUESTED,
    RULE_CUST_TENANT_SUSPENDED,
    RULE_CUST_TRIAL_EXPIRED,
    RULE_CUST_SUSPENDED,
    RULE_CUST_CONSENT_WITHDRAWN,
    RULE_CUST_NOT_VERIFIED,
    RULE_CUST_VERIFIED,
    RULE_CUST_TRIAL_EXPIRING,
    RULE_CUST_ACTIVE,
]


# --- Tests ---


def test_cust_banned_disabled() -> None:
    """Customer banned -> disabled, actions include revoke_all_sessions and purge_api_keys."""
    facts = _facts(subject=_subject(status='banned'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'revoke_all_sessions' in decision.actions
    assert 'purge_api_keys' in decision.actions
    assert any(r.rule_id == 'cust_banned' for r in decision.reasons)


def test_cust_deletion_requested_grace() -> None:
    """Customer deletion_requested -> grace, actions and signals from that rule."""
    facts = _facts(subject=_subject(status='deletion_requested'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.grace
    assert 'revoke_new_sessions' in decision.actions
    assert 'schedule_data_deletion' in decision.actions
    assert 'gdpr_deletion_pending' in decision.signals
    assert any(r.rule_id == 'cust_deletion_requested' for r in decision.reasons)


def test_cust_tenant_suspended() -> None:
    """Customer with tenant_status=suspended -> suspended, signal tenant_suspension."""
    facts = _facts(subject=_subject(tenant_status='suspended'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'tenant_suspension' in decision.signals
    assert any(r.rule_id == 'cust_tenant_suspended' for r in decision.reasons)


def test_cust_trial_expired() -> None:
    """Customer with expired trial initiative -> disabled, actions and signals from rule."""
    expired_trial = _trial_initiative(valid_until=NOW - timedelta(days=1))
    facts = _facts(target=_target(initiatives=[expired_trial]))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'show_upgrade_prompt' in decision.actions
    assert 'trial_expired' in decision.signals
    assert any(r.rule_id == 'cust_trial_expired' for r in decision.reasons)


def test_cust_suspended() -> None:
    """Customer suspended -> suspended, action restrict_to_readonly."""
    facts = _facts(subject=_subject(status='suspended'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'restrict_to_readonly' in decision.actions
    assert any(r.rule_id == 'cust_suspended' for r in decision.reasons)


def test_cust_consent_withdrawn() -> None:
    """Customer with required_consents_met=False -> disabled, signal consent_violation."""
    facts = _facts(subject=_subject(required_consents_met=False))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'consent_violation' in decision.signals
    assert any(r.rule_id == 'cust_consent_withdrawn' for r in decision.reasons)


def test_cust_not_verified() -> None:
    """Customer registered + email_verified=False -> pending, action send_verification_email."""
    facts = _facts(subject=_subject(status='registered', email_verified=False))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.pending
    assert 'send_verification_email' in decision.actions
    assert any(r.rule_id == 'cust_not_verified' for r in decision.reasons)


def test_cust_verified_enabled() -> None:
    """Customer with status=verified -> enabled."""
    facts = _facts(subject=_subject(status='verified'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert any(r.rule_id == 'cust_verified' for r in decision.reasons)


def test_cust_trial_expiring_signal() -> None:
    """Customer active + trial expiring within 7d -> signal trial_expiring_soon, state enabled."""
    expiring_trial = _trial_initiative(valid_until=NOW + timedelta(days=3))
    facts = _facts(target=_target(initiatives=[expiring_trial]))
    decision = evaluate(ALL_RULES, facts)
    assert 'trial_expiring_soon' in decision.signals
    # cust_trial_expiring has no abstract_state; cust_active (precedence 10) sets enabled
    assert decision.abstract_state == AbstractState.enabled
    assert any(r.rule_id == 'cust_trial_expiring' for r in decision.reasons)
    assert any(r.rule_id == 'cust_active' for r in decision.reasons)


def test_cust_active_enabled() -> None:
    """Customer active -> enabled (default happy path)."""
    facts = _facts()
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert any(r.rule_id == 'cust_active' for r in decision.reasons)


def test_cust_banned_precedence_over_suspended() -> None:
    """Customer with both banned+suspended rules: disabled wins (precedence 100 > 80)."""
    facts = _facts(subject=_subject(status='banned'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'cust_banned' for r in decision.reasons)
    assert not any(r.rule_id == 'cust_suspended' for r in decision.reasons)


def test_cust_tenant_suspended_with_active_status() -> None:
    """Customer active + tenant_status=suspended -> suspended (precedence 90 > 10)."""
    facts = _facts(subject=_subject(status='active', tenant_status='suspended'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert any(r.rule_id == 'cust_tenant_suspended' for r in decision.reasons)
    assert any(r.rule_id == 'cust_active' for r in decision.reasons)


def test_cust_consent_withdrawn_with_verified() -> None:
    """Customer verified + required_consents_met=False -> disabled (precedence 75 > 40)."""
    facts = _facts(subject=_subject(status='verified', required_consents_met=False))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'consent_violation' in decision.signals
    assert any(r.rule_id == 'cust_consent_withdrawn' for r in decision.reasons)
    assert any(r.rule_id == 'cust_verified' for r in decision.reasons)


def test_cust_no_matching_rule_fallback() -> None:
    """Customer with unknown status -> suspended + signal no_matching_rule."""
    facts = _facts(subject=_subject(status='unknown'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals
