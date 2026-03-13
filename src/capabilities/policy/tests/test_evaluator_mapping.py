# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — Mapping Stage (Phase 6, Step 11)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from src.capabilities.policy.evaluator import evaluate
from src.capabilities.policy.schemas import (
    AbstractState,
    Facts,
    Initiative,
    OwnerFacts,
    Rule,
    SubjectFacts,
    TargetFacts,
    ThreatFacts,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Mapping table (mirrors TASK.md spec)
# ---------------------------------------------------------------------------

MAPPING: dict[str, dict[str, Any]] = {
    'ad': {
        'enabled': {'concrete': 'userAccountControl=512', 'actions': ['ensure_account', 'enable']},
        'disabled': {'concrete': 'userAccountControl=514', 'actions': ['disable']},
        'suspended': {'concrete': 'userAccountControl=514', 'actions': ['disable', 'notify_manager']},
        'grace': {'concrete': 'userAccountControl=512', 'actions': ['enable', 'schedule_disable']},
        'pending': {'concrete': None, 'actions': []},
    },
    'jira': {
        'enabled': {'concrete': 'active', 'actions': ['ensure_account']},
        'disabled': {'concrete': 'inactive', 'actions': ['revoke_access']},
        'pending': {'concrete': 'inactive', 'actions': []},
    },
    'github': {
        'enabled': {'concrete': 'member', 'actions': ['ensure_membership']},
        'disabled': {'concrete': 'removed', 'actions': ['remove_membership']},
    },
    'stripe_billing': {
        'enabled': {'concrete': 'active', 'actions': ['ensure_subscription']},
        'disabled': {'concrete': 'canceled', 'actions': ['cancel_subscription']},
        'suspended': {'concrete': 'past_due', 'actions': ['restrict_to_readonly', 'send_payment_reminder']},
        'grace': {'concrete': 'canceling', 'actions': ['allow_data_export', 'schedule_cancel']},
        'pending': {'concrete': 'incomplete', 'actions': []},
    },
    'customer_portal': {
        'enabled': {'concrete': 'active', 'actions': ['ensure_access']},
        'disabled': {'concrete': 'blocked', 'actions': ['revoke_access']},
        'suspended': {'concrete': 'readonly', 'actions': ['set_readonly']},
        'grace': {'concrete': 'export_only', 'actions': ['enable_data_export']},
        'pending': {'concrete': 'verify_email', 'actions': ['show_verification_page']},
    },
}

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _employee(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'emp-1', 'kind': 'employee', 'status': 'active'}
    base.update(overrides)
    return SubjectFacts(**base)


def _nhi(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'nhi-1', 'kind': 'nhi', 'status': 'active'}
    base.update(overrides)
    return SubjectFacts(**base)


def _customer(**overrides: object) -> SubjectFacts:
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
    base: dict = {'application': 'ad'}
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


def _grace_initiative(valid_until: datetime) -> Initiative:
    return Initiative(type='grace', valid_until=valid_until)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

RULE_ACTIVE_ENABLE = Rule(
    id='active_enable',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

RULE_EMP_TERM_WINS = Rule(
    id='emp_term_wins',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'terminated'},
    then={'abstract_state': 'disabled', 'actions': ['revoke_all_sessions']},
    precedence=100,
)

RULE_EMP_LEAVE_BIRTHRIGHT_SUSPEND = Rule(
    id='emp_leave_birthright_suspend',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'on_leave'},
    then={'abstract_state': 'suspended', 'actions': ['suspend_birthright_access']},
    precedence=80,
)

RULE_EMP_LEAVE_REQUESTED_CANCEL = Rule(
    id='emp_leave_requested_cancel_attestation',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'on_leave'},
    then={'abstract_state': 'disabled', 'actions': ['cancel_attestation']},
    precedence=80,
)

RULE_EMP_PRE_HIRE_NOT_YET = Rule(
    id='emp_pre_hire_not_yet',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'hired', 'subject.start_date': '> now'},
    then={
        'abstract_state': 'pending',
        'actions': [{'schedule_evaluation_at': 'subject.start_date'}],
    },
    precedence=50,
)

RULE_GRACE_ACTIVE = Rule(
    id='grace_active',
    kind='lifecycle',
    when={
        'target.has_initiative': 'grace',
        'target.initiative.grace.valid_until': '> now',
    },
    then={'abstract_state': 'grace'},
    precedence=110,
)

RULE_NHI_OWNER_TERMINATED = Rule(
    id='nhi_owner_terminated',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.owner.status': 'terminated'},
    then={'abstract_state': 'disabled', 'actions': ['revoke_nhi_access']},
    precedence=90,
)

RULE_CUST_BANNED = Rule(
    id='cust_banned',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'banned'},
    then={'abstract_state': 'disabled', 'actions': ['revoke_all_sessions', 'purge_api_keys']},
    precedence=100,
)

RULE_CUST_NOT_VERIFIED = Rule(
    id='cust_not_verified',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'active', 'subject.email_verified': False},
    then={'abstract_state': 'pending', 'actions': ['send_verification_email']},
    precedence=60,
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
    then={'abstract_state': 'suspended', 'signals': ['tenant_suspension']},
    precedence=90,
)

RULE_RISK_CREDENTIAL_COMPROMISED = Rule(
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

RULE_IDP_TERMINATED = Rule(
    id='idp_terminated',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'terminated', 'target': None},
    then={'abstract_state': 'disabled', 'actions': ['revoke_all_sessions']},
    precedence=100,
)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_mapping_ad_enabled() -> None:
    """Employee active + AD target -> abstract enabled -> concrete userAccountControl=512."""
    facts = _facts(subject=_employee(status='active'), target=_target(application='ad'))
    decision = evaluate([RULE_ACTIVE_ENABLE], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.concrete_state == 'userAccountControl=512'
    assert 'ensure_account' in decision.actions
    assert 'enable' in decision.actions


def test_mapping_ad_disabled() -> None:
    """Employee terminated + AD target -> abstract disabled -> concrete userAccountControl=514."""
    facts = _facts(subject=_employee(status='terminated'), target=_target(application='ad'))
    decision = evaluate([RULE_EMP_TERM_WINS], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state == 'userAccountControl=514'
    assert 'disable' in decision.actions


def test_mapping_ad_suspended() -> None:
    """Employee on_leave + AD birthright target -> abstract suspended -> concrete userAccountControl=514."""
    facts = _facts(subject=_employee(status='on_leave'), target=_target(application='ad'))
    decision = evaluate([RULE_EMP_LEAVE_BIRTHRIGHT_SUSPEND], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.suspended
    assert decision.concrete_state == 'userAccountControl=514'
    assert 'disable' in decision.actions
    assert 'notify_manager' in decision.actions


def test_mapping_ad_grace() -> None:
    """Employee terminated + grace initiative (valid) -> grace wins (prec 110 > 100) -> AD concrete."""
    valid_until = NOW + timedelta(days=7)
    facts = _facts(
        subject=_employee(status='terminated'),
        target=_target(application='ad', initiatives=[_grace_initiative(valid_until)]),
    )
    decision = evaluate([RULE_EMP_TERM_WINS, RULE_GRACE_ACTIVE], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.grace
    assert decision.concrete_state == 'userAccountControl=512'
    assert 'enable' in decision.actions
    assert 'schedule_disable' in decision.actions


def test_mapping_ad_pending() -> None:
    """Employee pre-hire + AD target -> abstract pending -> concrete None, lifecycle action resolved."""
    start = NOW + timedelta(days=14)
    facts = _facts(
        subject=_employee(status='hired', start_date=start),
        target=_target(application='ad'),
    )
    decision = evaluate([RULE_EMP_PRE_HIRE_NOT_YET], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.pending
    assert decision.concrete_state is None
    # Lifecycle action with resolved reference
    sched_action = next(
        (a for a in decision.actions if isinstance(a, dict) and 'schedule_evaluation_at' in a),
        None,
    )
    assert sched_action is not None
    assert sched_action['schedule_evaluation_at'] == start.isoformat()


def test_mapping_jira_disabled() -> None:
    """Employee on_leave + Jira requested target -> abstract disabled -> concrete inactive."""
    facts = _facts(subject=_employee(status='on_leave'), target=_target(application='jira'))
    decision = evaluate([RULE_EMP_LEAVE_REQUESTED_CANCEL], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state == 'inactive'
    assert 'revoke_access' in decision.actions


def test_mapping_github_disabled_nhi() -> None:
    """NHI with owner terminated + GitHub target -> abstract disabled -> concrete removed."""
    facts = _facts(
        subject=_nhi(owner=OwnerFacts(id='owner-1', status='terminated')),
        target=_target(application='github'),
    )
    decision = evaluate([RULE_NHI_OWNER_TERMINATED], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state == 'removed'
    assert 'remove_membership' in decision.actions


def test_mapping_github_grace_fallback() -> None:
    """NHI + grace initiative + GitHub target -> abstract grace, but GitHub has no grace entry -> fallback."""
    valid_until = NOW + timedelta(days=5)
    facts = _facts(
        subject=_nhi(owner=OwnerFacts(id='owner-1', status='terminated')),
        target=_target(application='github', initiatives=[_grace_initiative(valid_until)]),
    )
    decision = evaluate([RULE_NHI_OWNER_TERMINATED, RULE_GRACE_ACTIVE], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.grace
    # GitHub has no "grace" entry -> fallback
    assert decision.concrete_state is None
    # No mapping actions, but lifecycle actions preserved
    assert 'revoke_nhi_access' in decision.actions


def test_mapping_stripe_billing_disabled() -> None:
    """Customer banned + stripe_billing target -> disabled -> concrete canceled."""
    facts = _facts(
        subject=_customer(status='banned'),
        target=_target(application='stripe_billing'),
    )
    decision = evaluate([RULE_CUST_BANNED], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state == 'canceled'
    # Lifecycle actions
    assert 'revoke_all_sessions' in decision.actions
    assert 'purge_api_keys' in decision.actions
    # Mapping actions
    assert 'cancel_subscription' in decision.actions


def test_mapping_customer_portal_pending() -> None:
    """Customer not verified + customer_portal target -> pending -> concrete verify_email."""
    facts = _facts(
        subject=_customer(status='active', email_verified=False),
        target=_target(application='customer_portal'),
    )
    decision = evaluate([RULE_CUST_NOT_VERIFIED], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.pending
    assert decision.concrete_state == 'verify_email'
    assert 'send_verification_email' in decision.actions
    assert 'show_verification_page' in decision.actions


def test_mapping_customer_portal_grace() -> None:
    """Customer deletion_requested + customer_portal -> grace -> concrete export_only."""
    facts = _facts(
        subject=_customer(status='deletion_requested'),
        target=_target(application='customer_portal'),
    )
    decision = evaluate([RULE_CUST_DELETION_REQUESTED], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.grace
    assert decision.concrete_state == 'export_only'
    assert 'enable_data_export' in decision.actions


def test_mapping_customer_portal_suspended() -> None:
    """Customer with tenant_status=suspended + customer_portal -> suspended -> concrete readonly."""
    facts = _facts(
        subject=_customer(tenant_status='suspended'),
        target=_target(application='customer_portal'),
    )
    decision = evaluate([RULE_CUST_TENANT_SUSPENDED], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.suspended
    assert decision.concrete_state == 'readonly'
    assert 'set_readonly' in decision.actions


def test_mapping_skip_when_target_none() -> None:
    """IDP rule (target=None) + mapping provided -> mapping NOT applied, concrete_state stays None."""
    facts = _facts(
        subject=_employee(status='terminated'),
        target=None,
    )
    decision = evaluate([RULE_IDP_TERMINATED], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state is None
    assert 'revoke_all_sessions' in decision.actions


def test_mapping_none_parameter() -> None:
    """mapping=None -> mapping stage skipped, concrete_state stays None (backward compat)."""
    facts = _facts(subject=_employee(status='active'), target=_target(application='ad'))
    decision = evaluate([RULE_ACTIVE_ENABLE], facts, mapping=None)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.concrete_state is None
    assert 'ensure_account' not in decision.actions


def test_mapping_unknown_application() -> None:
    """Application not in mapping table -> fallback: concrete_state None, no mapping actions."""
    facts = _facts(
        subject=_employee(status='active'),
        target=_target(application='unknown_app'),
    )
    decision = evaluate([RULE_ACTIVE_ENABLE], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.concrete_state is None
    # No mapping actions injected
    assert decision.actions == []


def test_mapping_risk_plus_mapping() -> None:
    """Risk rule (prec 300) wins over lifecycle (prec 10) -> disabled -> AD concrete + risk actions."""
    facts = _facts(
        subject=_employee(status='active'),
        target=_target(application='ad'),
        threat=_threat(active_indicators=['credential_compromised']),
    )
    decision = evaluate([RULE_ACTIVE_ENABLE, RULE_RISK_CREDENTIAL_COMPROMISED], facts, mapping=MAPPING)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state == 'userAccountControl=514'
    # Risk actions first
    assert 'force_password_reset' in decision.actions
    assert 'revoke_all_sessions' in decision.actions
    # Mapping actions present
    assert 'disable' in decision.actions


def test_mapping_actions_appended_after_lifecycle_risk() -> None:
    """Mapping actions come AFTER lifecycle/risk actions in the final list."""
    facts = _facts(
        subject=_employee(status='terminated'),
        target=_target(application='ad'),
    )
    decision = evaluate([RULE_EMP_TERM_WINS], facts, mapping=MAPPING)
    # revoke_all_sessions is a lifecycle action, disable is a mapping action
    lc_idx = decision.actions.index('revoke_all_sessions')
    map_idx = decision.actions.index('disable')
    assert lc_idx < map_idx, 'Mapping actions must appear after lifecycle/risk actions'


def test_mapping_actions_not_resolved() -> None:
    """Mapping actions are static strings — dot notation in them is NOT resolved."""
    # Use a mapping where one action contains a dot (looks like a reference path)
    tricky_mapping: dict[str, dict[str, Any]] = {
        'ad': {
            'enabled': {
                'concrete': 'userAccountControl=512',
                'actions': ['subject.start_date'],  # dot in action — must stay as-is
            },
        },
    }
    facts = _facts(
        subject=_employee(status='active', start_date=NOW),
        target=_target(application='ad'),
    )
    decision = evaluate([RULE_ACTIVE_ENABLE], facts, mapping=tricky_mapping)
    assert decision.abstract_state == AbstractState.enabled
    # Action must be the literal string, not a resolved value
    assert 'subject.start_date' in decision.actions
