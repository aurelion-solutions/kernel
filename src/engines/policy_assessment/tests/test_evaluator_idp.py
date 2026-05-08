# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — IDP / Subject-Level Evaluation (Phase 6, Step 5)."""

from datetime import UTC, datetime, timedelta

from src.engines.policy_assessment.schemas import (
    AbstractState,
    Facts,
    OwnerFacts,
    Rule,
    SubjectFacts,
    TargetFacts,
)
from src.engines.policy_assessment.strategies.deterministic.evaluator import evaluate

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Rule definitions — IDP lifecycle subset (employee + NHI)
# ---------------------------------------------------------------------------

RULE_IDP_EMP_TERMINATED = Rule(
    id='idp_emp_terminated',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'terminated',
        'target': None,
    },
    then={
        'abstract_state': 'disabled',
        'actions': ['revoke_all_sessions', 'disable_idp_account'],
    },
    precedence=100,
)

RULE_IDP_EMP_ON_LEAVE = Rule(
    id='idp_emp_on_leave',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'on_leave',
        'target': None,
    },
    then={
        'abstract_state': 'suspended',
        'actions': ['revoke_all_sessions'],
    },
    precedence=50,
)

RULE_IDP_EMP_PRE_HIRE = Rule(
    id='idp_emp_pre_hire',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'hired',
        'subject.start_date': '> now',
        'target': None,
    },
    then={
        'abstract_state': 'pending',
        'actions': [{'schedule_create_idp_account': 'subject.start_date'}],
    },
    precedence=40,
)

RULE_IDP_EMP_HIRE_DAY = Rule(
    id='idp_emp_hire_day',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'hired',
        'subject.start_date': '<= now',
        'target': None,
    },
    then={
        'abstract_state': 'enabled',
        'actions': ['create_idp_account', 'send_welcome_email'],
    },
    precedence=40,
)

RULE_IDP_NHI_OWNER_TERMINATED = Rule(
    id='idp_nhi_owner_terminated',
    kind='lifecycle',
    when={
        'subject.kind': 'nhi',
        'subject.owner.status': 'terminated',
        'target': None,
    },
    then={
        'abstract_state': 'disabled',
        'actions': ['revoke_all_tokens', 'disable_nhi_credentials'],
    },
    precedence=100,
)

RULE_IDP_NHI_EXPIRED = Rule(
    id='idp_nhi_expired',
    kind='lifecycle',
    when={
        'subject.kind': 'nhi',
        'subject.expires_at': '<= now',
        'target': None,
    },
    then={
        'abstract_state': 'disabled',
        'actions': ['revoke_all_tokens'],
    },
    precedence=95,
)

# A regular per-target rule (no target: null) — used in guard regression tests.
RULE_EMP_TERM_WINS = Rule(
    id='emp_term_wins',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'terminated'},
    then={'abstract_state': 'disabled', 'actions': ['disable_account']},
    precedence=100,
)

ALL_IDP_RULES = [
    RULE_IDP_EMP_TERMINATED,
    RULE_IDP_EMP_ON_LEAVE,
    RULE_IDP_EMP_PRE_HIRE,
    RULE_IDP_EMP_HIRE_DAY,
    RULE_IDP_NHI_OWNER_TERMINATED,
    RULE_IDP_NHI_EXPIRED,
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _employee(**overrides: object) -> SubjectFacts:
    base: dict = {
        'id': 'emp-1',
        'kind': 'employee',
        'status': 'active',
    }
    base.update(overrides)
    return SubjectFacts(**base)


def _nhi(**overrides: object) -> SubjectFacts:
    base: dict = {
        'id': 'nhi-1',
        'kind': 'nhi',
        'status': 'active',
        'owner': OwnerFacts(id='owner-1', status='active'),
    }
    base.update(overrides)
    return SubjectFacts(**base)


def _facts_no_target(subject: SubjectFacts) -> Facts:
    return Facts(subject=subject, target=None, now=NOW)


def _facts_with_target(subject: SubjectFacts) -> Facts:
    return Facts(subject=subject, target=TargetFacts(application='app-1'), now=NOW)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_idp_emp_terminated_disabled() -> None:
    """Employee terminated, target=None -> disabled + expected actions."""
    facts = _facts_no_target(_employee(status='terminated'))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state is None
    assert 'revoke_all_sessions' in decision.actions
    assert 'disable_idp_account' in decision.actions
    assert any(r.rule_id == 'idp_emp_terminated' for r in decision.reasons)


def test_idp_emp_on_leave_suspended() -> None:
    """Employee on_leave, target=None -> suspended + revoke_all_sessions."""
    facts = _facts_no_target(_employee(status='on_leave'))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert decision.concrete_state is None
    assert 'revoke_all_sessions' in decision.actions
    assert any(r.rule_id == 'idp_emp_on_leave' for r in decision.reasons)


def test_idp_emp_pre_hire_pending() -> None:
    """Employee hired with start_date in the future, target=None -> pending + scheduled action."""
    future = NOW + timedelta(days=10)
    facts = _facts_no_target(_employee(status='hired', start_date=future))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.pending
    assert decision.concrete_state is None
    scheduled = {'schedule_create_idp_account': future.isoformat()}
    assert scheduled in decision.actions
    assert any(r.rule_id == 'idp_emp_pre_hire' for r in decision.reasons)


def test_idp_emp_hire_day_enabled() -> None:
    """Employee hired with start_date <= now, target=None -> enabled + create/welcome actions."""
    past = NOW - timedelta(days=1)
    facts = _facts_no_target(_employee(status='hired', start_date=past))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert decision.concrete_state is None
    assert 'create_idp_account' in decision.actions
    assert 'send_welcome_email' in decision.actions
    assert any(r.rule_id == 'idp_emp_hire_day' for r in decision.reasons)


def test_idp_nhi_owner_terminated_disabled() -> None:
    """NHI with owner.status=terminated, target=None -> disabled + token/cred actions."""
    facts = _facts_no_target(_nhi(owner=OwnerFacts(id='owner-1', status='terminated')))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state is None
    assert 'revoke_all_tokens' in decision.actions
    assert 'disable_nhi_credentials' in decision.actions
    assert any(r.rule_id == 'idp_nhi_owner_terminated' for r in decision.reasons)


def test_idp_nhi_expired_disabled() -> None:
    """NHI with expires_at in the past, target=None -> disabled + revoke_all_tokens."""
    facts = _facts_no_target(_nhi(expires_at=NOW - timedelta(days=1)))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state is None
    assert 'revoke_all_tokens' in decision.actions
    assert any(r.rule_id == 'idp_nhi_expired' for r in decision.reasons)


def test_idp_rule_no_match_with_target_present() -> None:
    """IDP rule (target: null) must NOT match when facts.target is a TargetFacts."""
    facts = _facts_with_target(_employee(status='terminated'))
    # Only IDP rules in the set — none should fire.
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals
    assert decision.reasons == []


def test_per_target_rule_no_match_with_target_none() -> None:
    """Regular per-target rule must NOT match when facts.target is None."""
    facts = _facts_no_target(_employee(status='terminated'))
    # Only the per-target rule — should not fire.
    decision = evaluate([RULE_EMP_TERM_WINS], facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals
    assert decision.reasons == []


def test_idp_emp_terminated_precedence_over_on_leave() -> None:
    """Both terminated + on_leave IDP rules could match — terminated wins (100 > 50)."""
    # Create a subject that satisfies BOTH rules' non-target conditions.
    # Since 'status' can only be one value, we need a rule set where terminated fires.
    # Terminated has precedence 100, on_leave has 50.
    facts = _facts_no_target(_employee(status='terminated'))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'idp_emp_terminated' for r in decision.reasons)


def test_idp_no_matching_rule_fallback() -> None:
    """target=None with subject status that matches no IDP rule -> suspended + no_matching_rule."""
    facts = _facts_no_target(_employee(status='active'))
    decision = evaluate(ALL_IDP_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals


def test_existing_employee_rules_still_work_with_target() -> None:
    """Regression: per-target emp_term_wins still fires when facts.target is present."""
    facts = _facts_with_target(_employee(status='terminated'))
    decision = evaluate([RULE_EMP_TERM_WINS], facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'disable_account' in decision.actions
    assert any(r.rule_id == 'emp_term_wins' for r in decision.reasons)
