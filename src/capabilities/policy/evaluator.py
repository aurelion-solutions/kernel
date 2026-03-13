# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PDP Evaluator — employee lifecycle with initiative support."""

from datetime import datetime, timedelta
import re
from typing import Any

from src.capabilities.policy.schemas import (
    AbstractState,
    Decision,
    Facts,
    Reason,
    RiskLevel,
    Rule,
)


def _resolve_path(facts: Facts, path: str) -> Any:
    """Resolve a dotted path like 'subject.start_date' against a Facts object.

    Special case: 'target.initiative.<type>.<field>' finds the first Initiative
    in target.initiatives where initiative.type == <type> and returns its <field>.
    """
    parts = path.split('.')
    if parts == ['target', 'has_initiative'] and facts.target is not None:
        return [init.type for init in facts.target.initiatives]

    if parts == ['threat', 'has_indicator']:
        if facts.threat is None:
            return []
        return facts.threat.active_indicators

    # Handle target.initiative.<type>.<field> (4 segments: target, initiative, type, field)
    if len(parts) == 4 and parts[0] == 'target' and parts[1] == 'initiative' and facts.target is not None:
        init_type = parts[2]
        field = parts[3]
        for init in facts.target.initiatives:
            if init.type == init_type:
                return getattr(init, field, None)
        return None

    obj: Any = facts
    for part in parts:
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    return obj


def _resolve_action_references(action: Any, facts: Facts) -> Any:
    """Resolve dotted path references inside action dicts.

    Resolved values are converted to str to conform to Action = str | dict[str, str].
    """
    if isinstance(action, dict):
        resolved = {}
        for k, v in action.items():
            if isinstance(v, str) and '.' in v:
                val = _resolve_path(facts, v)
                resolved[k] = val.isoformat() if hasattr(val, 'isoformat') else str(val)
            else:
                resolved[k] = v
        return resolved
    return action


_RANGE_PATTERN = re.compile(r'^(now(?:[+-]\d+d)?)\.\.(now(?:[+-]\d+d)?)$')
_BOUND_PATTERN = re.compile(r'^now([+-])(\d+)d$')
_NUMERIC_RANGE_PATTERN = re.compile(r'^(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)$')


def _parse_range_bound(token: str, now: datetime) -> datetime:
    """Parse a range bound token like 'now', 'now+7d', 'now-3d' relative to now."""
    if token == 'now':
        return now
    m = _BOUND_PATTERN.match(token)
    if not m:
        raise ValueError(f'Invalid range bound: {token!r}')
    sign, days = m.group(1), int(m.group(2))
    delta = timedelta(days=days)
    return now + delta if sign == '+' else now - delta


def _coerce_bool(value: Any) -> Any:
    """Coerce string 'true'/'false' from YAML to Python bool."""
    if isinstance(value, str):
        low = value.strip().lower()
        if low == 'true':
            return True
        if low == 'false':
            return False
    return value


def _is_null_value(value: Any) -> bool:
    """Return True if value represents a null condition (None or string 'null')."""
    return value is None or (isinstance(value, str) and value.strip() == 'null')


def match_condition(facts: Facts, key: str, value: Any) -> bool:
    """Check a single condition against facts.

    Supports:
    - Exact equality: key resolves to value
    - Temporal: value is "> now" or "<= now" (compare resolved field against facts.now)
    - Null check: value is None or string "null" — resolved path must be None
    - Range: value matches "now..now+Nd" — left-inclusive, right-exclusive date range
    - has_initiative: target.has_initiative checks target.initiatives list membership
    - has_indicator: threat.has_indicator checks threat.active_indicators list membership
    - Numeric: value is "> N" (e.g. "> 10") — compares resolved int/float field against N
    """
    if key == 'target.has_initiative':
        if facts.target is None:
            return False
        return any(init.type == value for init in facts.target.initiatives)

    if key == 'threat.has_indicator':
        if facts.threat is None:
            return False
        return value in facts.threat.active_indicators

    # Null check operator
    if _is_null_value(value):
        return _resolve_path(facts, key) is None

    if not isinstance(value, str):
        return _resolve_path(facts, key) == _coerce_bool(value)

    sval = value.strip()

    # Temporal operators
    if sval in ('> now', '>= now', '< now', '<= now'):
        resolved = _resolve_path(facts, key)
        if resolved is None or not isinstance(resolved, datetime):
            return False
        now = facts.now
        if sval == '> now':
            return resolved > now
        if sval == '>= now':
            return resolved >= now
        if sval == '< now':
            return resolved < now
        return resolved <= now

    # Range operator: "now..now+Nd" or "now-Nd..now" etc.
    m = _RANGE_PATTERN.match(sval)
    if m:
        resolved = _resolve_path(facts, key)
        if resolved is None or not isinstance(resolved, datetime):
            return False
        left = _parse_range_bound(m.group(1), facts.now)
        right = _parse_range_bound(m.group(2), facts.now)
        return left <= resolved < right

    # Numeric range operator: "N..M" (e.g., "0.7..0.9") — left-inclusive, right-exclusive
    nm = _NUMERIC_RANGE_PATTERN.match(sval)
    if nm:
        resolved = _resolve_path(facts, key)
        if resolved is None or not isinstance(resolved, (int, float)):
            return False
        left = float(nm.group(1))
        right = float(nm.group(2))
        return left <= float(resolved) < right

    # Numeric comparison: "> N" (e.g., "> 10" or "> 0.9")
    if sval.startswith('> '):
        threshold_str = sval[2:].strip()
        try:
            threshold = float(threshold_str)
        except ValueError:
            return False
        resolved = _resolve_path(facts, key)
        if resolved is None or not isinstance(resolved, (int, float)):
            return False
        return float(resolved) > threshold

    return _resolve_path(facts, key) == _coerce_bool(value)


def _rule_has_target_null(rule: Rule) -> bool:
    """Return True if the rule has ``target: null`` (or ``target: None``) in its when dict."""
    return 'target' in rule.when and _is_null_value(rule.when['target'])


def match_rule(rule: Rule, facts: Facts) -> bool:
    """Return True if ALL conditions in rule.when match the given facts.

    Target guard (strict partition):
    - Rules with ``target: null`` in when only match when facts.target is None.
    - Rules without ``target: null`` only match when facts.target is not None.
    """
    is_idp_rule = _rule_has_target_null(rule)
    if is_idp_rule and facts.target is not None:
        return False
    if not is_idp_rule and facts.target is None:
        return False
    # Skip the 'target' key itself — the guard above already enforced it.
    return all(match_condition(facts, k, v) for k, v in rule.when.items() if k != 'target')


def _resolve_fact_value(facts: Facts, key: str) -> Any:
    """Resolve a condition key to its actual fact value for audit trail purposes.

    Handles virtual keys (target.has_initiative, target: null) that have no
    direct attribute on Facts, returning a meaningful representation.
    """
    if key in ('target.has_initiative', 'threat.has_indicator'):
        return _resolve_path(facts, key) or []
    if key == 'target':
        return facts.target
    return _resolve_path(facts, key)


def _pick_risk_level(matched: list[Rule]) -> RiskLevel | None:
    """Return RiskLevel from the highest-precedence matched rule that sets it, or None."""
    risk_rules = [r for r in matched if 'risk_level' in r.then]
    if not risk_rules:
        return None
    return RiskLevel(max(risk_rules, key=lambda r: r.precedence).then['risk_level'])


def _apply_mapping(
    abstract_state: AbstractState,
    facts: Facts,
    mapping: dict[str, dict[str, Any]],
) -> tuple[str | None, list[str]]:
    """Look up mapping table for (application, abstract_state).

    Returns (concrete_state, mapping_actions).
    Falls back to (None, []) when application or abstract_state not found.
    Mapping actions are static strings — do NOT resolve references.
    """
    if facts.target is None:
        return None, []
    app_mapping = mapping.get(facts.target.application)
    if app_mapping is None:
        return None, []
    entry = app_mapping.get(str(abstract_state))
    if entry is None:
        return None, []
    return entry.get('concrete'), list(entry.get('actions', []))


def evaluate(
    rules: list[Rule],
    facts: Facts,
    mapping: dict[str, dict[str, Any]] | None = None,
) -> Decision:
    """Evaluate rules against facts, returning a Decision.

    - Filters to matched rules.
    - Picks abstract_state from highest-precedence rule that sets it.
    - Tie-break: same precedence + different abstract_state -> suspended + precedence_conflict.
    - Accumulates actions/signals from all matched rules.
    - Falls back to suspended + no_matching_rule when nothing matches.
    - If mapping is provided and facts.target is not None, translates abstract_state
      to concrete_state and appends application-specific actions (static, not resolved).
    """
    matched: list[Rule] = [r for r in rules if match_rule(r, facts)]

    if not matched:
        return Decision(
            abstract_state=AbstractState.suspended,
            signals=['no_matching_rule'],
        )

    # Collect actions and signals from all matched rules
    all_actions: list[Any] = []
    all_signals: list[str] = []
    reasons: list[Reason] = []

    for r in matched:
        then = r.then
        if 'actions' in then:
            for a in then['actions']:
                all_actions.append(_resolve_action_references(a, facts))
        if 'signals' in then:
            all_signals.extend(then['signals'])
        reasons.append(
            Reason(
                rule_id=r.id,
                rule_kind=r.kind,
                precedence=r.precedence,
                matched_conditions={k: str(v) for k, v in r.when.items()},
                fact_values={k: str(_resolve_fact_value(facts, k)) for k in r.when},
                produced={k: str(v) for k, v in then.items()},
            )
        )

    # Determine abstract_state from highest-precedence rule(s) that set it
    state_rules = [r for r in matched if 'abstract_state' in r.then]

    if not state_rules:
        return Decision(
            abstract_state=AbstractState.suspended,
            risk_level=_pick_risk_level(matched),
            actions=all_actions,
            signals=[*all_signals, 'no_matching_rule'],
            reasons=reasons,
        )

    max_prec = max(r.precedence for r in state_rules)
    top_rules = [r for r in state_rules if r.precedence == max_prec]
    top_states = {r.then['abstract_state'] for r in top_rules}

    if len(top_states) > 1:
        abstract_state = AbstractState.suspended
        all_signals.append('precedence_conflict')
    else:
        abstract_state = AbstractState(top_states.pop())

    # Mapping stage: translate abstract_state -> concrete_state + app-specific actions
    concrete_state: str | None = None
    if mapping is not None and facts.target is not None:
        concrete_state, mapping_actions = _apply_mapping(abstract_state, facts, mapping)
        all_actions.extend(mapping_actions)

    return Decision(
        abstract_state=abstract_state,
        concrete_state=concrete_state,
        risk_level=_pick_risk_level(matched),
        actions=all_actions,
        signals=all_signals,
        reasons=reasons,
    )
