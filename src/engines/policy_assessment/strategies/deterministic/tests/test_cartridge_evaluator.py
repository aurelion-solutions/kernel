# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the deterministic cartridge condition evaluator."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput, PolicyAssessmentRequest
from src.engines.policy_assessment.dispatcher import PolicyAssessmentDispatcher
from src.engines.policy_assessment.schemas import AbstractState, RiskLevel
from src.engines.policy_assessment.strategies.deterministic.cartridge_evaluator import (
    _eval_node,
    _lookup,
    evaluate_deterministic_cartridge,
)
from src.inventory.policy.enums import AssessmentStrategy, PolicyType

# ---------------------------------------------------------------------------
# _lookup
# ---------------------------------------------------------------------------


def test_lookup_simple_key() -> None:
    assert _lookup({'status': 'active'}, 'status') == 'active'


def test_lookup_nested_path() -> None:
    ctx = {'subject': {'status': 'terminated'}}
    assert _lookup(ctx, 'subject.status') == 'terminated'


def test_lookup_missing_top_key_returns_none() -> None:
    assert _lookup({}, 'subject') is None


def test_lookup_missing_nested_key_returns_none() -> None:
    ctx = {'subject': {'id': 's-1'}}
    assert _lookup(ctx, 'subject.status') is None


def test_lookup_none_intermediate_returns_none() -> None:
    ctx = {'subject': None}
    assert _lookup(ctx, 'subject.status') is None


def test_lookup_non_dict_intermediate_returns_none() -> None:
    ctx = {'subject': 'string_value'}
    assert _lookup(ctx, 'subject.status') is None


# ---------------------------------------------------------------------------
# _eval_node — individual operators
# ---------------------------------------------------------------------------


def test_equals_match() -> None:
    node = {'equals': {'fact': 'subject.status', 'value': 'terminated'}}
    ctx = {'subject': {'status': 'terminated'}}
    assert _eval_node(node, ctx) is True


def test_equals_no_match() -> None:
    node = {'equals': {'fact': 'subject.status', 'value': 'terminated'}}
    ctx = {'subject': {'status': 'active'}}
    assert _eval_node(node, ctx) is False


def test_equals_missing_fact_returns_false() -> None:
    node = {'equals': {'fact': 'subject.status', 'value': 'terminated'}}
    assert _eval_node(node, {}) is False


def test_not_equals_match() -> None:
    node = {'not_equals': {'fact': 'subject.status', 'value': 'active'}}
    ctx = {'subject': {'status': 'terminated'}}
    assert _eval_node(node, ctx) is True


def test_not_equals_no_match() -> None:
    node = {'not_equals': {'fact': 'subject.status', 'value': 'active'}}
    ctx = {'subject': {'status': 'active'}}
    assert _eval_node(node, ctx) is False


def test_is_null_string_form_match() -> None:
    node = {'is_null': 'subject.owner'}
    assert _eval_node(node, {'subject': {'owner': None}}) is True


def test_is_null_string_form_no_match() -> None:
    node = {'is_null': 'subject.owner'}
    assert _eval_node(node, {'subject': {'owner': 'emp-1'}}) is False


def test_is_null_dict_form_match() -> None:
    node = {'is_null': {'fact': 'subject.owner'}}
    assert _eval_node(node, {'subject': {}}) is True


def test_is_null_missing_fact_treated_as_none() -> None:
    node = {'is_null': 'subject.owner'}
    assert _eval_node(node, {}) is True


def test_is_not_null_match() -> None:
    node = {'is_not_null': 'subject.id'}
    assert _eval_node(node, {'subject': {'id': 's-1'}}) is True


def test_is_not_null_no_match_when_none() -> None:
    node = {'is_not_null': 'subject.id'}
    assert _eval_node(node, {'subject': {'id': None}}) is False


def test_is_not_null_dict_form() -> None:
    node = {'is_not_null': {'fact': 'subject.id'}}
    assert _eval_node(node, {'subject': {'id': 's-1'}}) is True


def test_greater_than_match() -> None:
    node = {'greater_than': {'fact': 'days_inactive', 'value': 90}}
    assert _eval_node(node, {'days_inactive': 120}) is True


def test_greater_than_no_match() -> None:
    node = {'greater_than': {'fact': 'days_inactive', 'value': 90}}
    assert _eval_node(node, {'days_inactive': 30}) is False


def test_greater_than_equal_is_not_greater() -> None:
    node = {'greater_than': {'fact': 'score', 'value': 0.9}}
    assert _eval_node(node, {'score': 0.9}) is False


def test_greater_than_missing_fact_returns_false() -> None:
    node = {'greater_than': {'fact': 'score', 'value': 0.5}}
    assert _eval_node(node, {}) is False


def test_greater_than_accepts_float_context_value() -> None:
    node = {'greater_than': {'fact': 'score', 'value': 0.7}}
    assert _eval_node(node, {'score': 0.95}) is True


def test_greater_than_string_ish_numbers_and_none() -> None:
    """Production coercion behaviour for greater_than against non-numeric inputs.

    Production short-circuits ``None`` to ``False`` (cartridge_evaluator.py lines 66–67)
    and catches ``TypeError|ValueError`` from ``float()`` (lines 70–71).
    Non-numeric strings → ``False``.
    Numeric strings (``'123'``) coerce via ``float()`` and compare numerically.
    Do NOT modify cartridge_evaluator.py to change this behaviour; any change
    is a production code mutation out of scope for Phase 17 Step 15.
    """
    node = {'greater_than': {'fact': 'x', 'value': 100}}

    # (a) String-ish numeric above threshold: float('123') = 123.0 > 100 → True
    assert _eval_node(node, {'x': '123'}) is True

    # (b) String-ish numeric below threshold: float('50') = 50.0 > 100 → False
    assert _eval_node(node, {'x': '50'}) is False

    # (c) Non-numeric string → ValueError caught by float() → False
    assert _eval_node(node, {'x': 'abc'}) is False

    # (d) None value → explicit short-circuit at line 66–67 → False
    assert _eval_node(node, {'x': None}) is False


# ---------------------------------------------------------------------------
# _eval_node — all / any combinators
# ---------------------------------------------------------------------------


def test_all_both_true() -> None:
    node = {
        'all': [
            {'equals': {'fact': 'a', 'value': 1}},
            {'equals': {'fact': 'b', 'value': 2}},
        ]
    }
    assert _eval_node(node, {'a': 1, 'b': 2}) is True


def test_all_one_false() -> None:
    node = {
        'all': [
            {'equals': {'fact': 'a', 'value': 1}},
            {'equals': {'fact': 'b', 'value': 99}},
        ]
    }
    assert _eval_node(node, {'a': 1, 'b': 2}) is False


def test_all_empty_is_true() -> None:
    assert _eval_node({'all': []}, {}) is True


def test_any_one_true() -> None:
    node = {
        'any': [
            {'equals': {'fact': 'a', 'value': 99}},
            {'equals': {'fact': 'b', 'value': 2}},
        ]
    }
    assert _eval_node(node, {'a': 1, 'b': 2}) is True


def test_any_all_false() -> None:
    node = {
        'any': [
            {'equals': {'fact': 'a', 'value': 99}},
            {'equals': {'fact': 'b', 'value': 99}},
        ]
    }
    assert _eval_node(node, {'a': 1, 'b': 2}) is False


def test_any_empty_is_false() -> None:
    assert _eval_node({'any': []}, {}) is False


def test_nested_all_inside_any() -> None:
    node = {
        'any': [
            {'all': [{'equals': {'fact': 'x', 'value': 1}}, {'equals': {'fact': 'y', 'value': 2}}]},
            {'equals': {'fact': 'z', 'value': 3}},
        ]
    }
    assert _eval_node(node, {'x': 1, 'y': 2, 'z': 99}) is True
    assert _eval_node(node, {'x': 1, 'y': 99, 'z': 3}) is True
    assert _eval_node(node, {'x': 1, 'y': 99, 'z': 99}) is False


def test_deeply_nested_mixed_all_any() -> None:
    """4-level DSL tree: all → any → all → any, mixing equals/greater_than/is_null.

    Tree shape (real operator keys from cartridge_evaluator.py — no equal_to/less_than):

        all([
          any([
            all([
              any([
                {equals: subject.status == "terminated"},
                {is_null: "subject.owner"}
              ]),
              {greater_than: days_inactive > 90}
            ]),
            {equals: account_status == "active"}
          ]),
          {is_null: "subject.deletion_blocked_at"}
        ])

    Hand-verified truth tables for three input combinations; assertions use
    boolean identity (``is True`` / ``is False``), not smoke checks.
    """
    node: dict = {
        'all': [
            {
                'any': [
                    {
                        'all': [
                            {
                                'any': [
                                    {'equals': {'fact': 'subject.status', 'value': 'terminated'}},
                                    {'is_null': 'subject.owner'},
                                ]
                            },
                            {'greater_than': {'fact': 'days_inactive', 'value': 90}},
                        ]
                    },
                    {'equals': {'fact': 'account_status', 'value': 'active'}},
                ]
            },
            {'is_null': 'subject.deletion_blocked_at'},
        ]
    }

    # Combination 1 — all-true path:
    #   innermost any: equals(status=="terminated") → True (short-circuit)
    #   inner all: True AND greater_than(120 > 90)=True → True
    #   outer any: inner all=True (short-circuit) → True
    #   is_null(deletion_blocked_at missing) → True
    #   outer all → True
    ctx1 = {
        'subject': {'status': 'terminated', 'owner': 'emp-1'},
        'days_inactive': 120,
        'account_status': 'active',
    }
    assert _eval_node(node, ctx1) is True

    # Combination 2 — mid-tree short-circuit via outer any branch 2:
    #   innermost any: equals(status=="terminated")=False; is_null(owner=None)=True → True
    #   inner all: True AND greater_than(30 > 90)=False → False
    #   outer any: inner all=False → try branch 2: equals(account_status=="active")=True → True
    #   is_null(deletion_blocked_at missing) → True
    #   outer all → True
    ctx2 = {
        'subject': {'status': 'active', 'owner': None},
        'days_inactive': 30,
        'account_status': 'active',
    }
    assert _eval_node(node, ctx2) is True

    # Combination 3 — all-false path (deletion_blocked_at present, account_status disabled):
    #   inner all: innermost any False (status not terminated, owner present) AND
    #              greater_than(10 > 90)=False → False
    #   outer any: inner all=False; equals(account_status=="disabled"!="active")=False → False
    #   outer all: outer any=False → short-circuit → False
    ctx3 = {
        'subject': {
            'status': 'active',
            'owner': 'emp-1',
            'deletion_blocked_at': '2026-01-01',
        },
        'days_inactive': 10,
        'account_status': 'disabled',
    }
    assert _eval_node(node, ctx3) is False


def test_unknown_node_raises() -> None:
    with pytest.raises(ValueError, match='Unknown DSL node keys'):
        _eval_node({'magic': True}, {})


# ---------------------------------------------------------------------------
# evaluate_deterministic_cartridge — full function
# ---------------------------------------------------------------------------


def _make_request(
    condition: dict[str, Any],
    context: dict[str, Any],
    decision: dict[str, Any] | None = None,
) -> PolicyAssessmentRequest:
    return PolicyAssessmentRequest(
        policy_type=PolicyType.ACCESS_RISK,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
        policy_id='test.cartridge',
        policy_definition={
            'id': 'test.cartridge',
            'rule_id': 'test.cartridge.rule',
            'condition': condition,
            'decision': decision or {'action': 'flag_for_review', 'risk_level': 'high'},
        },
        context=context,
    )


def test_matched_condition_returns_matched_true() -> None:
    req = _make_request(
        condition={'equals': {'fact': 'subject.status', 'value': 'terminated'}},
        context={'subject': {'status': 'terminated'}},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.matched is True


def test_non_matched_condition_returns_matched_false() -> None:
    req = _make_request(
        condition={'equals': {'fact': 'subject.status', 'value': 'terminated'}},
        context={'subject': {'status': 'active'}},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.matched is False
    assert result.decision is None


def test_missing_fact_treated_as_none_no_match() -> None:
    req = _make_request(
        condition={'equals': {'fact': 'subject.status', 'value': 'terminated'}},
        context={},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.matched is False


def test_missing_fact_is_null_matches() -> None:
    req = _make_request(
        condition={'is_null': 'subject.owner'},
        context={},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.matched is True


def test_orphaned_access_style_match() -> None:
    req = _make_request(
        condition={
            'all': [
                {'equals': {'fact': 'subject_not_found', 'value': True}},
                {'equals': {'fact': 'account_status', 'value': 'active'}},
            ]
        },
        context={'subject_not_found': True, 'account_status': 'active'},
        decision={'action': 'flag_for_review', 'risk_level': 'high'},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.matched is True
    assert result.decision is not None
    assert result.decision.risk_level == RiskLevel.high
    assert 'flag_for_review' in result.decision.actions


def test_decision_risk_level_mapped() -> None:
    req = _make_request(
        condition={'equals': {'fact': 'x', 'value': 1}},
        context={'x': 1},
        decision={'risk_level': 'critical'},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.decision is not None
    assert result.decision.risk_level == RiskLevel.critical


def test_decision_abstract_state_defaults_to_suspended() -> None:
    req = _make_request(
        condition={'equals': {'fact': 'x', 'value': 1}},
        context={'x': 1},
        decision={},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.decision is not None
    assert result.decision.abstract_state == AbstractState.suspended


def test_decision_abstract_state_can_be_set_explicitly() -> None:
    req = _make_request(
        condition={'equals': {'fact': 'x', 'value': 1}},
        context={'x': 1},
        decision={'abstract_state': 'disabled'},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.decision is not None
    assert result.decision.abstract_state == AbstractState.disabled


def test_payload_contains_id_and_rule_id() -> None:
    req = _make_request(
        condition={'equals': {'fact': 'x', 'value': 1}},
        context={'x': 1},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.payload.get('id') == 'test.cartridge'
    assert result.payload.get('rule_id') == 'test.cartridge.rule'


def test_empty_condition_returns_not_matched() -> None:
    req = PolicyAssessmentRequest(
        policy_type=PolicyType.ACCESS_RISK,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
        policy_definition={'condition': {}, 'decision': {}},
        context={'x': 1},
    )
    result = evaluate_deterministic_cartridge(req)
    assert result.matched is False


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------


def test_dispatcher_routes_to_cartridge_evaluator_when_condition_present() -> None:
    dispatcher = PolicyAssessmentDispatcher(policy_service=MagicMock())
    req = PolicyAssessmentRequest(
        policy_type=PolicyType.ACCESS_RISK,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
        policy_definition={
            'id': 'test',
            'rule_id': 'test.rule',
            'condition': {'equals': {'fact': 'status', 'value': 'terminated'}},
            'decision': {'risk_level': 'high'},
        },
        context={'status': 'terminated'},
    )
    result = dispatcher.evaluate(req)
    assert isinstance(result, PolicyAssessmentOutput)
    assert result.matched is True


def test_dispatcher_preserves_old_behavior_without_condition() -> None:
    from datetime import UTC, datetime

    from src.engines.policy_assessment.schemas import AbstractState, Decision, Facts, SubjectFacts

    mock_service = MagicMock()
    mock_service.evaluate_policy.return_value = Decision(abstract_state=AbstractState.enabled)
    dispatcher = PolicyAssessmentDispatcher(policy_service=mock_service)

    facts = Facts(
        subject=SubjectFacts(id='s-1', kind='employee', status='active'),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    req = PolicyAssessmentRequest(
        policy_type=PolicyType.LIFECYCLE,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
        policy_definition={},
        context=facts.model_dump(),
    )
    result = dispatcher.evaluate(req)
    mock_service.evaluate_policy.assert_called_once()
    assert result.matched is True
