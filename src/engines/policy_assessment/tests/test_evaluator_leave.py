# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PDP Evaluator — Full Employee Leave Scenario Integration Test (Phase 6, Step 12).

Simulates the real-world process: employee "emp-42" goes on leave and returns,
triggering evaluations across AD, Jira, GitHub, and IDP (target=None).
Each test calls evaluate() with the full ALL_RULES list and MAPPING dict.
"""

from datetime import UTC, datetime
from typing import Any

from src.engines.policy_assessment.schemas import (
    AbstractState,
    Facts,
    Initiative,
    OwnerFacts,
    Rule,
    SubjectFacts,
    TargetFacts,
)
from src.engines.policy_assessment.strategies.deterministic.evaluator import evaluate

NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Mapping table (AD, Jira, GitHub)
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
}

# ---------------------------------------------------------------------------
# Rules — full rule set (all 8 rules used in every test)
# ---------------------------------------------------------------------------

# Active employee fallback (prec 10)
RULE_ACTIVE_ENABLE = Rule(
    id='active_enable',
    kind='lifecycle',
    when={'subject.kind': 'employee', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

# Leave + birthright -> suspended (prec 52)
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

# Leave + requested -> disabled + reattest_on_return (prec 50)
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

# Leave + requested + has_pending_attestation -> cancel action only (no abstract_state, prec 51)
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

# Return reattestation needed (prec 60)
RULE_RETURN_REATTEST = Rule(
    id='emp_return_reattest',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'active',
        'target.pending_reattestation': True,
    },
    then={'abstract_state': 'pending', 'actions': ['create_attestation_task']},
    precedence=60,
)

# IDP / subject-level: employee on_leave (target: null, prec 50)
RULE_IDP_EMP_ON_LEAVE = Rule(
    id='idp_emp_on_leave',
    kind='lifecycle',
    when={
        'subject.kind': 'employee',
        'subject.status': 'on_leave',
        'target': None,
    },
    then={'abstract_state': 'suspended', 'actions': ['revoke_all_sessions']},
    precedence=50,
)

# NHI: owner on_leave -> suspended (prec 50)
RULE_NHI_OWNER_ON_LEAVE = Rule(
    id='nhi_owner_on_leave',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.owner.status': 'on_leave'},
    then={'abstract_state': 'suspended'},
    precedence=50,
)

# NHI active fallback (prec 10)
RULE_NHI_ACTIVE = Rule(
    id='nhi_active',
    kind='lifecycle',
    when={'subject.kind': 'nhi', 'subject.status': 'active'},
    then={'abstract_state': 'enabled'},
    precedence=10,
)

ALL_RULES: list[Rule] = [
    RULE_ACTIVE_ENABLE,
    RULE_LEAVE_BIRTHRIGHT,
    RULE_LEAVE_REQUESTED,
    RULE_LEAVE_CANCEL_ATTEST,
    RULE_RETURN_REATTEST,
    RULE_IDP_EMP_ON_LEAVE,
    RULE_NHI_OWNER_ON_LEAVE,
    RULE_NHI_ACTIVE,
]

# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _employee(**overrides: object) -> SubjectFacts:
    base: dict = {'id': 'emp-42', 'kind': 'employee', 'status': 'active'}
    base.update(overrides)
    return SubjectFacts(**base)


def _target(**overrides: object) -> TargetFacts:
    base: dict = {'application': 'ad'}
    base.update(overrides)
    return TargetFacts(**base)


def _facts(**overrides: object) -> Facts:
    base: dict = {'subject': _employee(), 'target': _target(), 'now': NOW}
    base.update(overrides)
    return Facts(**base)


# ---------------------------------------------------------------------------
# Phase 1 — Leave starts
# ---------------------------------------------------------------------------


def test_leave_ad_birthright_suspended() -> None:
    """Employee on_leave + AD birthright -> suspended, AD mapping applied."""
    facts = _facts(
        subject=_employee(status='on_leave'),
        target=_target(application='ad', initiatives=[Initiative(type='birthright')]),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.suspended
    assert decision.concrete_state == 'userAccountControl=514'
    assert 'disable' in decision.actions
    assert 'notify_manager' in decision.actions
    assert any(r.rule_id == 'emp_leave_birthright_suspend' for r in decision.reasons)


def test_leave_jira_requested_with_attestation() -> None:
    """Employee on_leave + Jira requested + has_pending_attestation=True.

    Both revoke and cancel-attestation rules fire; Jira disabled mapping applied;
    reattest_on_return signal present.
    """
    facts = _facts(
        subject=_employee(status='on_leave'),
        target=_target(
            application='jira',
            initiatives=[Initiative(type='requested')],
            has_pending_attestation=True,
        ),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state == 'inactive'
    assert 'cancel_pending_attestation' in decision.actions
    assert 'revoke_access' in decision.actions
    assert 'reattest_on_return' in decision.signals
    rule_ids = {r.rule_id for r in decision.reasons}
    assert 'emp_leave_requested_revoke' in rule_ids
    assert 'emp_leave_requested_cancel_attestation' in rule_ids


def test_leave_jira_requested_without_attestation() -> None:
    """Employee on_leave + Jira requested + has_pending_attestation=False.

    cancel_pending_attestation must NOT be in actions; reattest_on_return signal present.
    """
    facts = _facts(
        subject=_employee(status='on_leave'),
        target=_target(
            application='jira',
            initiatives=[Initiative(type='requested')],
            has_pending_attestation=False,
        ),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.disabled
    assert decision.concrete_state == 'inactive'
    assert 'cancel_pending_attestation' not in decision.actions
    assert 'revoke_access' in decision.actions
    assert 'reattest_on_return' in decision.signals


def test_leave_github_birthright_no_mapping_for_suspended() -> None:
    """Employee on_leave + GitHub birthright -> suspended, but GitHub has no suspended entry.

    concrete_state must be None; no mapping actions injected.
    """
    facts = _facts(
        subject=_employee(status='on_leave'),
        target=_target(application='github', initiatives=[Initiative(type='birthright')]),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.suspended
    assert decision.concrete_state is None
    # No mapping actions (GitHub suspended not in table)
    assert 'ensure_membership' not in decision.actions
    assert 'remove_membership' not in decision.actions


def test_leave_idp_subject_level() -> None:
    """Employee on_leave, target=None -> IDP subject-level rule fires; mapping skipped."""
    facts = _facts(
        subject=_employee(status='on_leave'),
        target=None,
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.suspended
    assert decision.concrete_state is None
    assert 'revoke_all_sessions' in decision.actions
    assert any(r.rule_id == 'idp_emp_on_leave' for r in decision.reasons)


# ---------------------------------------------------------------------------
# Phase 2 — Employee returns
# ---------------------------------------------------------------------------


def test_return_ad_birthright_enabled() -> None:
    """Employee active (returned) + AD birthright, pending_reattestation=False -> enabled."""
    facts = _facts(
        subject=_employee(status='active'),
        target=_target(
            application='ad',
            initiatives=[Initiative(type='birthright')],
            pending_reattestation=False,
        ),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.enabled
    assert decision.concrete_state == 'userAccountControl=512'
    assert 'ensure_account' in decision.actions
    assert 'enable' in decision.actions


def test_return_jira_requested_reattestation() -> None:
    """Employee active (returned) + Jira requested + pending_reattestation=True.

    emp_return_reattest (prec 60) beats active_enable (prec 10) -> pending.
    """
    facts = _facts(
        subject=_employee(status='active'),
        target=_target(
            application='jira',
            initiatives=[Initiative(type='requested')],
            pending_reattestation=True,
        ),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.pending
    assert decision.concrete_state == 'inactive'
    assert 'create_attestation_task' in decision.actions
    assert any(r.rule_id == 'emp_return_reattest' for r in decision.reasons)


def test_return_github_birthright_enabled() -> None:
    """Employee active (returned) + GitHub birthright, pending_reattestation=False -> enabled."""
    facts = _facts(
        subject=_employee(status='active'),
        target=_target(
            application='github',
            initiatives=[Initiative(type='birthright')],
            pending_reattestation=False,
        ),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.enabled
    assert decision.concrete_state == 'member'
    assert 'ensure_membership' in decision.actions


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_leave_dual_initiative_birthright_wins() -> None:
    """Employee on_leave + target has BOTH birthright AND requested.

    birthright rule (prec 52) wins over requested (prec 50) for abstract_state.
    Actions and signals from both rules are accumulated.
    reattest_on_return signal comes from the requested rule.
    """
    facts = _facts(
        subject=_employee(status='on_leave'),
        target=_target(
            application='ad',
            initiatives=[Initiative(type='birthright'), Initiative(type='requested')],
        ),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.suspended
    assert 'reattest_on_return' in decision.signals
    rule_ids = {r.rule_id for r in decision.reasons}
    assert 'emp_leave_birthright_suspend' in rule_ids
    assert 'emp_leave_requested_revoke' in rule_ids


def test_leave_cascades_to_owned_nhi() -> None:
    """NHI owned by on_leave employee -> nhi_owner_on_leave fires -> abstract_state suspended."""
    facts = _facts(
        subject=SubjectFacts(
            id='nhi-99',
            kind='nhi',
            status='active',
            owner=OwnerFacts(id='emp-42', status='on_leave'),
        ),
        target=_target(application='ad'),
    )
    decision = evaluate(ALL_RULES, facts, mapping=MAPPING)

    assert decision.abstract_state == AbstractState.suspended
    assert any(r.rule_id == 'nhi_owner_on_leave' for r in decision.reasons)
