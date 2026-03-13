# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — employee lifecycle WITH initiatives."""

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
    base: dict = {'id': 'emp-1', 'kind': 'employee', 'status': 'active'}
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


# --- Rules used across tests ---

# Base rule: terminated -> disabled (precedence 100)
RULE_TERM = Rule(
    id='emp_term_wins',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'terminated'},
    then={'abstract_state': 'disabled'},
    precedence=100,
)

# Grace active (precedence 110, beats termination)
RULE_GRACE = Rule(
    id='grace_active',
    kind='lifecycle',
    when={
        'target.has_initiative': 'grace',
        'target.initiative.grace.valid_until': '> now',
    },
    then={'abstract_state': 'grace'},
    precedence=110,
)

# Employee return reattestation
RULE_RETURN_REATTEST = Rule(
    id='emp_return_reattest',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'active',
        'target.pending_reattestation': True,
    },
    then={
        'abstract_state': 'pending',
        'actions': ['create_attestation_task'],
    },
    precedence=60,
)

# Leave + birthright -> suspended
RULE_LEAVE_BIRTHRIGHT = Rule(
    id='emp_leave_birthright_suspend',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'on_leave',
        'target.has_initiative': 'birthright',
    },
    then={'abstract_state': 'suspended'},
    precedence=52,
)

# Leave + requested -> disabled + reattest_on_return signal
RULE_LEAVE_REQUESTED = Rule(
    id='emp_leave_requested_revoke',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'on_leave',
        'target.has_initiative': 'requested',
    },
    then={'abstract_state': 'disabled', 'signals': ['reattest_on_return']},
    precedence=50,
)

# Leave + requested + pending attestation -> cancel attestation (no abstract_state)
RULE_LEAVE_CANCEL_ATTEST = Rule(
    id='emp_leave_requested_cancel_attestation',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'on_leave',
        'target.has_initiative': 'requested',
        'target.has_pending_attestation': True,
    },
    then={'actions': ['cancel_pending_attestation']},
    precedence=51,
)

# Active employee enabled (low precedence fallback)
RULE_ACTIVE = Rule(
    id='active_enable',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

ALL_RULES = [
    RULE_GRACE,
    RULE_TERM,
    RULE_RETURN_REATTEST,
    RULE_LEAVE_BIRTHRIGHT,
    RULE_LEAVE_CANCEL_ATTEST,
    RULE_LEAVE_REQUESTED,
    RULE_ACTIVE,
]


def test_grace_active_overrides_termination():
    """Terminated + grace initiative with valid_until > now -> grace (110 > 100)."""
    grace = Initiative(type='grace', valid_until=NOW + timedelta(days=30))
    facts = _facts(
        subject=_subject(status='terminated'),
        target=_target(initiatives=[grace]),
    )
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.grace
    assert any(r.rule_id == 'grace_active' for r in decision.reasons)


def test_grace_expired_falls_to_disabled():
    """Terminated + grace with valid_until <= now -> disabled (grace doesn't match)."""
    grace = Initiative(type='grace', valid_until=NOW - timedelta(days=1))
    facts = _facts(
        subject=_subject(status='terminated'),
        target=_target(initiatives=[grace]),
    )
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'emp_term_wins' for r in decision.reasons)
    assert not any(r.rule_id == 'grace_active' for r in decision.reasons)


def test_leave_birthright_suspended():
    """on_leave + birthright initiative -> suspended."""
    birthright = Initiative(type='birthright')
    facts = _facts(
        subject=_subject(status='on_leave'),
        target=_target(initiatives=[birthright]),
    )
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert any(r.rule_id == 'emp_leave_birthright_suspend' for r in decision.reasons)


def test_leave_requested_disabled():
    """on_leave + requested initiative -> disabled + reattest_on_return signal."""
    requested = Initiative(type='requested')
    facts = _facts(
        subject=_subject(status='on_leave'),
        target=_target(initiatives=[requested]),
    )
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'reattest_on_return' in decision.signals
    assert any(r.rule_id == 'emp_leave_requested_revoke' for r in decision.reasons)


def test_leave_requested_cancel_attestation():
    """on_leave + requested + has_pending_attestation -> cancel action, state disabled."""
    requested = Initiative(type='requested')
    facts = _facts(
        subject=_subject(status='on_leave'),
        target=_target(initiatives=[requested], has_pending_attestation=True),
    )
    decision = evaluate(ALL_RULES, facts)
    # State comes from emp_leave_requested_revoke (precedence 50)
    assert decision.abstract_state == AbstractState.disabled
    # Action from emp_leave_requested_cancel_attestation (precedence 51)
    assert 'cancel_pending_attestation' in decision.actions


def test_return_reattest_pending():
    """Active + pending_reattestation -> pending + create_attestation_task."""
    facts = _facts(
        subject=_subject(status='active'),
        target=_target(pending_reattestation=True),
    )
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.pending
    assert 'create_attestation_task' in decision.actions
    assert any(r.rule_id == 'emp_return_reattest' for r in decision.reasons)


def test_leave_multiple_initiatives_birthright_wins():
    """on_leave + birthright AND requested -> suspended (52 > 50)."""
    birthright = Initiative(type='birthright')
    requested = Initiative(type='requested')
    facts = _facts(
        subject=_subject(status='on_leave'),
        target=_target(initiatives=[birthright, requested]),
    )
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    # Both rules should match
    rule_ids = {r.rule_id for r in decision.reasons}
    assert 'emp_leave_birthright_suspend' in rule_ids
    assert 'emp_leave_requested_revoke' in rule_ids


def test_has_initiative_no_match():
    """on_leave + delegated initiative (no matching rule) -> no state rule matches."""
    delegated = Initiative(type='delegated')
    facts = _facts(
        subject=_subject(status='on_leave'),
        target=_target(initiatives=[delegated]),
    )
    decision = evaluate(ALL_RULES, facts)
    # No leave rule matches for delegated, no state-setting rule applies
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals


def test_initiative_field_resolution_in_reason():
    """fact_values in Reason correctly shows initiative.grace.valid_until."""
    valid_until = NOW + timedelta(days=30)
    grace = Initiative(type='grace', valid_until=valid_until)
    facts = _facts(
        subject=_subject(status='terminated'),
        target=_target(initiatives=[grace]),
    )
    decision = evaluate(ALL_RULES, facts)
    grace_reason = next(r for r in decision.reasons if r.rule_id == 'grace_active')
    assert grace_reason.fact_values['target.initiative.grace.valid_until'] == str(valid_until)


def test_leave_requested_with_attestation_actions_accumulated():
    """Both cancel_pending_attestation and reattest_on_return are accumulated."""
    requested = Initiative(type='requested')
    facts = _facts(
        subject=_subject(status='on_leave'),
        target=_target(initiatives=[requested], has_pending_attestation=True),
    )
    decision = evaluate(ALL_RULES, facts)
    assert 'cancel_pending_attestation' in decision.actions
    assert 'reattest_on_return' in decision.signals
    # Both rules in reasons
    rule_ids = {r.rule_id for r in decision.reasons}
    assert 'emp_leave_requested_cancel_attestation' in rule_ids
    assert 'emp_leave_requested_revoke' in rule_ids


def test_has_initiative_fact_values_shows_types():
    """fact_values for target.has_initiative shows actual initiative types, not None."""
    grace = Initiative(type='grace', valid_until=NOW + timedelta(days=30))
    facts = _facts(
        subject=_subject(status='terminated'),
        target=_target(initiatives=[grace]),
    )
    decision = evaluate(ALL_RULES, facts)
    grace_reason = next(r for r in decision.reasons if r.rule_id == 'grace_active')
    assert grace_reason.fact_values['target.has_initiative'] == "['grace']"
