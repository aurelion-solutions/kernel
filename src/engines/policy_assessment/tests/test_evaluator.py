# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator Stage 1 — employee lifecycle without initiatives."""

from datetime import UTC, datetime, timedelta

from src.engines.policy_assessment.schemas import (
    AbstractState,
    Facts,
    Rule,
    SubjectFacts,
    TargetFacts,
)
from src.engines.policy_assessment.strategies.deterministic.evaluator import evaluate

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _subject(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'emp-1', 'kind': 'employee', 'status': 'active'}
    base.update(overrides)
    return SubjectFacts(**base)


def _target() -> TargetFacts:
    return TargetFacts(application='app-1')


def _facts(**overrides: object) -> Facts:
    base: dict = {'subject': _subject(), 'target': _target(), 'now': NOW}
    base.update(overrides)
    return Facts(**base)


# Standard employee lifecycle rules
RULES = [
    Rule(
        id='emp_term_wins',
        kind='lifecycle',
        when={'subject.kind': 'employee', 'subject.status': 'terminated'},
        then={'abstract_state': 'disabled'},
        precedence=100,
    ),
    Rule(
        id='emp_pre_hire_not_yet',
        kind='lifecycle',
        when={'subject.kind': 'employee', 'subject.status': 'hired', 'subject.start_date': '> now'},
        then={'abstract_state': 'pending', 'actions': [{'schedule_evaluation_at': 'subject.start_date'}]},
        precedence=40,
    ),
    Rule(
        id='emp_hire_day_enable',
        kind='lifecycle',
        when={'subject.kind': 'employee', 'subject.status': 'hired', 'subject.start_date': '<= now'},
        then={'abstract_state': 'enabled'},
        precedence=40,
    ),
    Rule(
        id='active_enable',
        kind='lifecycle',
        when={'subject.kind': 'employee', 'subject.status': 'active'},
        then={'abstract_state': 'enabled'},
        precedence=10,
    ),
]


# --- 1. test_employee_terminated_disabled ---


def test_employee_terminated_disabled():
    facts = _facts(subject=_subject(status='terminated'))
    decision = evaluate(RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'emp_term_wins' for r in decision.reasons)


# --- 2. test_employee_active_enabled ---


def test_employee_active_enabled():
    facts = _facts()
    decision = evaluate(RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert any(r.rule_id == 'active_enable' for r in decision.reasons)


# --- 3. test_employee_pre_hire_pending ---


def test_employee_pre_hire_pending():
    future = NOW + timedelta(days=10)
    facts = _facts(subject=_subject(status='hired', start_date=future))
    decision = evaluate(RULES, facts)
    assert decision.abstract_state == AbstractState.pending
    assert any(a == {'schedule_evaluation_at': future.isoformat()} for a in decision.actions)


# --- 4. test_employee_hire_day_enabled ---


def test_employee_hire_day_enabled():
    facts = _facts(subject=_subject(status='hired', start_date=NOW))
    decision = evaluate(RULES, facts)
    assert decision.abstract_state == AbstractState.enabled


# --- 5. test_precedence_term_over_active ---


def test_precedence_term_over_active():
    """If somehow both terminated and active match, terminated (100) wins over active (10)."""
    # Create a contrived scenario where both rules match
    rules = [
        Rule(
            id='r_high',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'abstract_state': 'disabled'},
            precedence=100,
        ),
        Rule(
            id='r_low',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'abstract_state': 'enabled'},
            precedence=10,
        ),
    ]
    facts = _facts()
    decision = evaluate(rules, facts)
    assert decision.abstract_state == AbstractState.disabled


# --- 6. test_no_matching_rule_fallback ---


def test_no_matching_rule_fallback():
    facts = _facts(subject=_subject(status='unknown_status'))
    decision = evaluate(RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals


# --- 7. test_actions_accumulated ---


def test_actions_accumulated():
    """Multiple matching rules -> actions from all are collected."""
    rules = [
        Rule(
            id='r1',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'abstract_state': 'enabled', 'actions': ['notify_manager']},
            precedence=10,
        ),
        Rule(
            id='r2',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'actions': ['send_welcome']},
            precedence=5,
        ),
    ]
    facts = _facts()
    decision = evaluate(rules, facts)
    assert 'notify_manager' in decision.actions
    assert 'send_welcome' in decision.actions


# --- 8. test_reasons_contain_all_matched_rules ---


def test_reasons_contain_all_matched_rules():
    rules = [
        Rule(
            id='r1',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'abstract_state': 'enabled'},
            precedence=10,
        ),
        Rule(
            id='r2',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'signals': ['audit']},
            precedence=5,
        ),
    ]
    facts = _facts()
    decision = evaluate(rules, facts)
    rule_ids = {r.rule_id for r in decision.reasons}
    assert rule_ids == {'r1', 'r2'}


# --- 9. test_action_reference_resolution ---


def test_action_reference_resolution():
    """{schedule_evaluation_at: subject.start_date} resolves to ISO string."""
    future = NOW + timedelta(days=7)
    facts = _facts(subject=_subject(status='hired', start_date=future))
    decision = evaluate(RULES, facts)
    resolved_action = {'schedule_evaluation_at': future.isoformat()}
    assert resolved_action in decision.actions


# --- 10. test_precedence_conflict_same_level ---


def test_precedence_conflict_same_level():
    """Two rules at same precedence with different abstract_state -> suspended + precedence_conflict."""
    rules = [
        Rule(
            id='r1',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'abstract_state': 'enabled'},
            precedence=50,
        ),
        Rule(
            id='r2',
            kind='lifecycle',
            when={'subject.kind': 'employee'},
            then={'abstract_state': 'disabled'},
            precedence=50,
        ),
    ]
    facts = _facts()
    decision = evaluate(rules, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'precedence_conflict' in decision.signals
