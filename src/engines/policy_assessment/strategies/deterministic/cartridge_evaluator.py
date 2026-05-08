# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Deterministic cartridge condition evaluator.

Evaluates request.policy_definition["condition"] against request.context and
produces PolicyAssessmentOutput. Operates on raw dicts — no Facts schema required.

Supported DSL nodes:
  all: list[node]                        — all sub-nodes must match
  any: list[node]                        — at least one sub-node must match
  equals: {fact: str, value: Any}        — fact == value
  not_equals: {fact: str, value: Any}    — fact != value
  is_null: str | {fact: str}             — fact is None
  is_not_null: str | {fact: str}         — fact is not None
  greater_than: {fact: str, value: num}  — float(fact) > float(value)
"""

from __future__ import annotations

from typing import Any

from src.engines.policy_assessment.contracts import PolicyAssessmentOutput, PolicyAssessmentRequest
from src.engines.policy_assessment.schemas import AbstractState, Decision, RiskLevel


def _lookup(context: dict[str, Any], path: str) -> Any:
    """Dot-path lookup from a plain dict context. Returns None for missing paths."""
    obj: Any = context
    for part in path.split('.'):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _eval_node(node: dict[str, Any], context: dict[str, Any]) -> bool:
    if 'all' in node:
        return all(_eval_node(child, context) for child in node['all'])

    if 'any' in node:
        return any(_eval_node(child, context) for child in node['any'])

    if 'equals' in node:
        spec = node['equals']
        return _lookup(context, spec['fact']) == spec['value']

    if 'not_equals' in node:
        spec = node['not_equals']
        return _lookup(context, spec['fact']) != spec['value']

    if 'is_null' in node:
        spec = node['is_null']
        path = spec if isinstance(spec, str) else spec['fact']
        return _lookup(context, path) is None

    if 'is_not_null' in node:
        spec = node['is_not_null']
        path = spec if isinstance(spec, str) else spec['fact']
        return _lookup(context, path) is not None

    if 'greater_than' in node:
        spec = node['greater_than']
        value = _lookup(context, spec['fact'])
        if value is None:
            return False
        try:
            return float(value) > float(spec['value'])
        except (TypeError, ValueError):
            return False

    raise ValueError(f'Unknown DSL node keys: {sorted(node.keys())}')


def _build_decision(policy_definition: dict[str, Any]) -> Decision:
    decision_dict = policy_definition.get('decision', {})

    raw_state = decision_dict.get('abstract_state', 'suspended')
    abstract_state = AbstractState(raw_state)

    raw_risk = decision_dict.get('risk_level')
    risk_level = RiskLevel(raw_risk) if raw_risk is not None else None

    if 'actions' in decision_dict:
        actions: list[Any] = list(decision_dict['actions'])
    elif 'action' in decision_dict:
        actions = [decision_dict['action']]
    else:
        actions = []

    return Decision(abstract_state=abstract_state, risk_level=risk_level, actions=actions)


def evaluate_deterministic_cartridge(request: PolicyAssessmentRequest) -> PolicyAssessmentOutput:
    """Evaluate request.policy_definition["condition"] against request.context."""
    policy_def = request.policy_definition
    condition = policy_def.get('condition')

    if not condition or not _eval_node(condition, request.context):
        return PolicyAssessmentOutput(matched=False, decision=None)

    decision = _build_decision(policy_def)
    payload: dict[str, Any] = {}
    if 'id' in policy_def:
        payload['id'] = policy_def['id']
    if 'rule_id' in policy_def:
        payload['rule_id'] = policy_def['rule_id']

    return PolicyAssessmentOutput(matched=True, decision=decision, payload=payload)
