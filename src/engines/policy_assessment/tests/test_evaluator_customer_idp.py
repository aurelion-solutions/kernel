# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — Customer IDP Rules (Subject-Level, Phase 6, Step 7)."""

from datetime import UTC, datetime

from src.engines.policy_assessment.schemas import (
    AbstractState,
    Facts,
    Rule,
    SubjectFacts,
    TargetFacts,
)
from src.engines.policy_assessment.strategies.deterministic.evaluator import evaluate

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Rule definitions — customer IDP subset (subject-level, target: null)
# ---------------------------------------------------------------------------

RULE_IDP_CUST_BANNED = Rule(
    id='idp_cust_banned',
    kind='lifecycle',
    when={
        'subject.kind': 'customer',
        'subject.status': 'banned',
        'target': None,
    },
    then={
        'abstract_state': 'disabled',
        'actions': ['revoke_all_sessions', 'revoke_all_api_keys', 'disable_customer_account'],
    },
    precedence=100,
)

RULE_IDP_CUST_DELETION = Rule(
    id='idp_cust_deletion',
    kind='lifecycle',
    when={
        'subject.kind': 'customer',
        'subject.status': 'deletion_requested',
        'target': None,
    },
    then={
        'abstract_state': 'grace',
        'actions': ['revoke_all_sessions', 'schedule_account_purge', 'send_deletion_confirmation'],
    },
    precedence=95,
)

RULE_IDP_CUST_SUSPENDED = Rule(
    id='idp_cust_suspended',
    kind='lifecycle',
    when={
        'subject.kind': 'customer',
        'subject.status': 'suspended',
        'target': None,
    },
    then={
        'abstract_state': 'suspended',
        'actions': ['revoke_all_sessions'],
    },
    precedence=80,
)

# Per-target customer rule (no target: null) — used in guard regression tests.
RULE_CUST_BANNED_PER_TARGET = Rule(
    id='cust_banned',
    kind='lifecycle',
    when={'subject.kind': 'customer', 'subject.status': 'banned'},
    then={
        'abstract_state': 'disabled',
        'actions': ['revoke_all_sessions', 'purge_api_keys'],
    },
    precedence=100,
)

ALL_IDP_CUST_RULES = [
    RULE_IDP_CUST_BANNED,
    RULE_IDP_CUST_DELETION,
    RULE_IDP_CUST_SUSPENDED,
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _customer(**overrides: object) -> SubjectFacts:
    base: dict = {
        'id': 'cust-1',
        'kind': 'customer',
        'status': 'active',
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


def test_idp_cust_banned_disabled() -> None:
    """Customer banned, target=None -> disabled + expected actions, concrete_state=None."""
    facts = _facts_no_target(_customer(status='banned'))
    decision = evaluate(ALL_IDP_CUST_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state is None
    assert 'revoke_all_sessions' in decision.actions
    assert 'revoke_all_api_keys' in decision.actions
    assert 'disable_customer_account' in decision.actions
    assert any(r.rule_id == 'idp_cust_banned' for r in decision.reasons)


def test_idp_cust_deletion_grace() -> None:
    """Customer deletion_requested, target=None -> grace + expected actions, concrete_state=None."""
    facts = _facts_no_target(_customer(status='deletion_requested'))
    decision = evaluate(ALL_IDP_CUST_RULES, facts)
    assert decision.abstract_state == AbstractState.grace
    assert decision.concrete_state is None
    assert 'revoke_all_sessions' in decision.actions
    assert 'schedule_account_purge' in decision.actions
    assert 'send_deletion_confirmation' in decision.actions
    assert any(r.rule_id == 'idp_cust_deletion' for r in decision.reasons)


def test_idp_cust_suspended_revoke() -> None:
    """Customer suspended, target=None -> suspended + revoke_all_sessions, concrete_state=None."""
    facts = _facts_no_target(_customer(status='suspended'))
    decision = evaluate(ALL_IDP_CUST_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert decision.concrete_state is None
    assert 'revoke_all_sessions' in decision.actions
    assert any(r.rule_id == 'idp_cust_suspended' for r in decision.reasons)


def test_idp_cust_banned_precedence_over_suspended() -> None:
    """Customer banned + both idp_cust_banned (100) and idp_cust_suspended (80) present.

    Status 'banned' only matches idp_cust_banned — result is disabled.
    """
    rules = [RULE_IDP_CUST_BANNED, RULE_IDP_CUST_SUSPENDED]
    facts = _facts_no_target(_customer(status='banned'))
    decision = evaluate(rules, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'idp_cust_banned' for r in decision.reasons)
    assert not any(r.rule_id == 'idp_cust_suspended' for r in decision.reasons)


def test_idp_cust_banned_precedence_over_deletion() -> None:
    """Customer banned + both idp_cust_banned (100) and idp_cust_deletion (95) present.

    Status 'banned' only matches idp_cust_banned — result is disabled.
    """
    rules = [RULE_IDP_CUST_BANNED, RULE_IDP_CUST_DELETION]
    facts = _facts_no_target(_customer(status='banned'))
    decision = evaluate(rules, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'idp_cust_banned' for r in decision.reasons)
    assert not any(r.rule_id == 'idp_cust_deletion' for r in decision.reasons)


def test_idp_cust_deletion_precedence_over_suspended() -> None:
    """Customer deletion_requested + both idp_cust_deletion (95) and idp_cust_suspended (80) present.

    Status 'deletion_requested' only matches idp_cust_deletion — result is grace.
    """
    rules = [RULE_IDP_CUST_DELETION, RULE_IDP_CUST_SUSPENDED]
    facts = _facts_no_target(_customer(status='deletion_requested'))
    decision = evaluate(rules, facts)
    assert decision.abstract_state == AbstractState.grace
    assert any(r.rule_id == 'idp_cust_deletion' for r in decision.reasons)
    assert not any(r.rule_id == 'idp_cust_suspended' for r in decision.reasons)


def test_idp_cust_rules_do_not_fire_with_target() -> None:
    """Customer banned + target is TargetFacts (not None).

    idp_cust_banned (target: null) must NOT match.
    With no matching per-target rule -> fallback: suspended + no_matching_rule.
    """
    facts = _facts_with_target(_customer(status='banned'))
    decision = evaluate(ALL_IDP_CUST_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals
    assert decision.reasons == []


def test_idp_cust_per_target_rules_do_not_fire_without_target() -> None:
    """Customer banned + target=None + per-target cust_banned rule (no target: null).

    The per-target rule must NOT match. Only idp_cust_banned (target: null) should match.
    """
    rules = [RULE_IDP_CUST_BANNED, RULE_CUST_BANNED_PER_TARGET]
    facts = _facts_no_target(_customer(status='banned'))
    decision = evaluate(rules, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'idp_cust_banned' for r in decision.reasons)
    assert not any(r.rule_id == 'cust_banned' for r in decision.reasons)


def test_idp_cust_no_matching_rule_fallback() -> None:
    """Customer unknown status, target=None, all 3 IDP rules present -> none match -> fallback."""
    facts = _facts_no_target(_customer(status='unknown'))
    decision = evaluate(ALL_IDP_CUST_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'no_matching_rule' in decision.signals
    assert decision.reasons == []
