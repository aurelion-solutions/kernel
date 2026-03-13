# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PDP Evaluator — NHI lifecycle operators and rules (Phase 6, Step 4)."""

from datetime import UTC, datetime, timedelta

from src.capabilities.policy.evaluator import evaluate
from src.capabilities.policy.schemas import (
    AbstractState,
    Facts,
    OwnerFacts,
    Rule,
    SubjectFacts,
    TargetFacts,
)

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)


def _owner(status: str = 'active') -> OwnerFacts:
    return OwnerFacts(id='owner-1', status=status)


def _subject(**overrides: object) -> SubjectFacts:
    base: dict = {
        'id': 'nhi-1',
        'kind': 'nhi',
        'status': 'active',
        'owner': _owner(),
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


# --- Rule definitions mirroring lifecycle.yaml NHI subset ---

RULE_NHI_OWNER_TERMINATED = Rule(
    id='nhi_owner_terminated',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.owner.status': 'terminated'},
    then={'abstract_state': 'disabled', 'signals': ['orphaned_nhi']},
    precedence=100,
)

RULE_NHI_EXPIRED = Rule(
    id='nhi_expired',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.expires_at': '<= now'},
    then={'abstract_state': 'disabled'},
    precedence=95,
)

RULE_NHI_LOCKED = Rule(
    id='nhi_locked',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.status': 'locked'},
    then={'abstract_state': 'disabled', 'signals': ['nhi_admin_locked']},
    precedence=92,
)

RULE_NHI_ORPHANED = Rule(
    id='nhi_orphaned',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.owner': None},
    then={'abstract_state': 'suspended', 'signals': ['orphaned_nhi_review']},
    precedence=90,
)

RULE_NHI_EXPIRING_SOON = Rule(
    id='nhi_expiring_soon',
    kind='lifecycle',
    when={
        'subject.kind': 'nhi',
        'subject.status': 'active',
        'subject.expires_at': 'now..now+30d',
    },
    then={'signals': ['rotation_needed']},
    precedence=80,
)

RULE_NHI_OWNER_ON_LEAVE = Rule(
    id='nhi_owner_on_leave',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.owner.status': 'on_leave'},
    then={'abstract_state': 'suspended'},
    precedence=50,
)

RULE_NHI_ACTIVE = Rule(
    id='nhi_active',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

ALL_RULES = [
    RULE_NHI_OWNER_TERMINATED,
    RULE_NHI_EXPIRED,
    RULE_NHI_LOCKED,
    RULE_NHI_ORPHANED,
    RULE_NHI_EXPIRING_SOON,
    RULE_NHI_OWNER_ON_LEAVE,
    RULE_NHI_ACTIVE,
]


def test_nhi_owner_terminated_disabled() -> None:
    """NHI with owner.status=terminated -> disabled + signal orphaned_nhi."""
    facts = _facts(subject=_subject(owner=_owner(status='terminated')))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'orphaned_nhi' in decision.signals
    assert any(r.rule_id == 'nhi_owner_terminated' for r in decision.reasons)


def test_nhi_expired_disabled() -> None:
    """NHI with expires_at in the past -> disabled."""
    facts = _facts(subject=_subject(expires_at=NOW - timedelta(days=1)))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'nhi_expired' for r in decision.reasons)


def test_nhi_locked_disabled() -> None:
    """NHI with status=locked -> disabled + signal nhi_admin_locked."""
    facts = _facts(subject=_subject(status='locked'))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert 'nhi_admin_locked' in decision.signals
    assert any(r.rule_id == 'nhi_locked' for r in decision.reasons)


def test_nhi_orphaned_suspended() -> None:
    """NHI with owner=None -> suspended + signal orphaned_nhi_review (null operator)."""
    facts = _facts(subject=_subject(owner=None))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert 'orphaned_nhi_review' in decision.signals
    assert any(r.rule_id == 'nhi_orphaned' for r in decision.reasons)


def test_nhi_expiring_soon_signal_only() -> None:
    """Active NHI with expires_at 15 days from now -> signal rotation_needed, state enabled."""
    facts = _facts(subject=_subject(expires_at=NOW + timedelta(days=15)))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert 'rotation_needed' in decision.signals
    assert any(r.rule_id == 'nhi_expiring_soon' for r in decision.reasons)
    assert any(r.rule_id == 'nhi_active' for r in decision.reasons)


def test_nhi_expiring_soon_already_expired_no_match() -> None:
    """NHI with expires_at in the past -> range now..now+30d does NOT match."""
    facts = _facts(subject=_subject(expires_at=NOW - timedelta(days=1)))
    decision = evaluate(ALL_RULES, facts)
    # nhi_expired fires, not nhi_expiring_soon
    assert decision.abstract_state == AbstractState.disabled
    assert 'rotation_needed' not in decision.signals
    assert not any(r.rule_id == 'nhi_expiring_soon' for r in decision.reasons)


def test_nhi_expiring_soon_far_future_no_match() -> None:
    """Active NHI with expires_at 60 days from now -> range now..now+30d does NOT match."""
    facts = _facts(subject=_subject(expires_at=NOW + timedelta(days=60)))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert 'rotation_needed' not in decision.signals
    assert not any(r.rule_id == 'nhi_expiring_soon' for r in decision.reasons)


def test_nhi_owner_on_leave_suspended() -> None:
    """NHI with owner.status=on_leave -> suspended."""
    facts = _facts(subject=_subject(owner=_owner(status='on_leave')))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.suspended
    assert any(r.rule_id == 'nhi_owner_on_leave' for r in decision.reasons)


def test_nhi_active_enabled() -> None:
    """Active NHI with active owner -> enabled (default case)."""
    facts = _facts()
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert any(r.rule_id == 'nhi_active' for r in decision.reasons)


def test_nhi_owner_terminated_beats_owner_on_leave() -> None:
    """owner.status=terminated beats on_leave: disabled (100) > suspended (50)."""
    facts = _facts(subject=_subject(owner=_owner(status='terminated')))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.disabled
    assert any(r.rule_id == 'nhi_owner_terminated' for r in decision.reasons)


def test_nhi_null_operator_non_null_no_match() -> None:
    """NHI with present owner -> subject.owner: null does NOT match."""
    # owner=_owner() is the default — not None
    facts = _facts(subject=_subject(owner=_owner()))
    # Run with only the orphaned rule so it's clear it doesn't fire
    decision = evaluate([RULE_NHI_ORPHANED, RULE_NHI_ACTIVE], facts)
    assert decision.abstract_state == AbstractState.enabled
    assert not any(r.rule_id == 'nhi_orphaned' for r in decision.reasons)


def test_nhi_expiring_soon_boundary_exact_30d() -> None:
    """NHI with expires_at exactly now+30d -> range now..now+30d does NOT match (right-exclusive)."""
    facts = _facts(subject=_subject(expires_at=NOW + timedelta(days=30)))
    decision = evaluate(ALL_RULES, facts)
    assert decision.abstract_state == AbstractState.enabled
    assert 'rotation_needed' not in decision.signals
    assert not any(r.rule_id == 'nhi_expiring_soon' for r in decision.reasons)
