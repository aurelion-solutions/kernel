# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""GenerativePDPService — stateless desired-state projection.

Given a full subject snapshot (SubjectContext + current_facts +
current_initiatives) this service returns the list of ProjectedFact
objects that represent the desired access state for that subject.

Two sources are merged:
1. **Birthright generation** — rules with ``kind: birthright`` in the
   loaded rule pack.  Each matched birthright rule emits one
   ProjectedFact with a fresh ``policy_rule:<rule_id>`` initiative.
2. **Carry-over** — existing initiatives of types ``requested``,
   ``delegated``, and ``grace`` (and any type with a future or open
   ``valid_until``) that have not expired and are not blocked by an
   employment / termination policy.

Design constraints (C1 invariant):
- This service NEVER reads the database.  All data is passed by value.
- ``context_overrides`` patches ``SubjectContext.attributes`` for
  what-if evaluation (dry-run path in access_plan).
- The caller (``access_plan``) is responsible for reading
  ``access_effective`` current_facts and ``inventory.initiatives``
  current_initiatives and passing them in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.engines.policy_assessment.generative.schemas import (
    CurrentFact,
    CurrentInitiative,
    InitiativeProjection,
    ProjectedFact,
    SubjectContext,
)
from src.engines.policy_assessment.schemas import (
    AbstractState,
    Decision,
    Reason,
    Rule,
    RulePack,
)
from src.inventory.initiatives.models import InitiativeType

# Initiative types eligible for carry-over by default.
# All 9 types are supported; these three are the primary carry-over candidates.
_CARRY_OVER_TYPES = frozenset(
    {
        InitiativeType.requested,
        InitiativeType.delegated,
        InitiativeType.grace,
        InitiativeType.inherited,
        InitiativeType.self_registered,
        InitiativeType.invited,
        InitiativeType.trial,
        InitiativeType.subscription,
    }
)

# Attribute key whose value represents employment status in SubjectContext.
_EMPLOYMENT_STATUS_KEY = 'employment_status'

# Employment status values that block all carry-over.
_BLOCKING_EMPLOYMENT_STATUSES = frozenset({'terminated', 'disabled'})


def _now_utc() -> datetime:
    return datetime.now(tz=UTC)


def _is_active_initiative(initiative: CurrentInitiative, now: datetime) -> bool:
    """Return True if the initiative is currently active (not expired)."""
    if initiative.valid_until is not None:
        until = initiative.valid_until
        if until.tzinfo is None:
            # naive datetime — treat as UTC
            until = until.replace(tzinfo=UTC)
        if until <= now:
            return False
    if initiative.valid_from is not None:
        from_dt = initiative.valid_from
        if from_dt.tzinfo is None:
            from_dt = from_dt.replace(tzinfo=UTC)
        if from_dt > now:
            return False
    return True


def _employment_blocks_carry_over(subject_context: SubjectContext) -> bool:
    """Return True if the subject's employment status blocks carry-over."""
    status = subject_context.attributes.get(_EMPLOYMENT_STATUS_KEY)
    if status is None:
        return False
    return str(status).lower() in _BLOCKING_EMPLOYMENT_STATUSES


def _match_birthright_rule(rule: Rule, subject_context: SubjectContext, now: datetime) -> bool:
    """Return True if all conditions in rule.when match the subject context.

    Birthright rules use a subset of condition keys:
      - subject.kind
      - subject.status  (mapped from SubjectContext.attributes['status'] /
                         ['employment_status'])
      - subject.org_unit
      - Any key from SubjectContext.attributes (e.g. attributes.department)
    """
    attrs = subject_context.attributes
    subject_status = str(attrs.get('status', attrs.get('employment_status', 'active')))

    for key, expected in rule.when.items():
        if key == 'subject.kind':
            if subject_context.subject_type != expected:
                return False
        elif key == 'subject.status':
            if subject_status != expected:
                return False
        elif key == 'subject.org_unit':
            if subject_context.org_unit_id != expected:
                return False
        elif key.startswith('attributes.'):
            attr_key = key[len('attributes.') :]
            actual = attrs.get(attr_key)
            if str(actual) != str(expected):
                return False
        else:
            # Unknown condition key — strict: fail the rule
            return False
    return True


def _build_birthright_decision(rule: Rule, subject_context: SubjectContext) -> Decision:
    """Build a Decision from a matched birthright rule's then block."""
    then = rule.then
    actions = list(then.get('actions', []))
    signals = list(then.get('signals', []))
    reason = Reason(
        rule_id=rule.id,
        rule_kind=rule.kind,
        precedence=rule.precedence,
        matched_conditions={k: str(v) for k, v in rule.when.items()},
        fact_values={
            'subject.kind': subject_context.subject_type,
            'subject.status': str(
                subject_context.attributes.get(
                    'status',
                    subject_context.attributes.get('employment_status', 'active'),
                )
            ),
        },
        produced={k: str(v) for k, v in then.items()},
    )
    return Decision(
        abstract_state=AbstractState.enabled,
        actions=actions,
        signals=signals,
        reasons=[reason],
    )


def _build_carry_over_decision(
    initiative: CurrentInitiative,
    existing_decision: Decision | None,
) -> Decision:
    """Build a Decision for a carried-over initiative."""
    reason = Reason(
        rule_id=f'carry_over:{initiative.type.value}',
        rule_kind='carry_over',
        precedence=0,
        matched_conditions={'initiative.type': initiative.type.value},
        fact_values={'initiative.origin': initiative.origin},
        produced={'carry_over': 'true'},
    )
    if existing_decision is not None:
        return Decision(
            abstract_state=existing_decision.abstract_state,
            concrete_state=existing_decision.concrete_state,
            risk_level=existing_decision.risk_level,
            actions=list(existing_decision.actions),
            signals=list(existing_decision.signals),
            reasons=[*existing_decision.reasons, reason],
        )
    return Decision(
        abstract_state=AbstractState.enabled,
        reasons=[reason],
    )


def _dedupe_key(application: str, target_descriptor: dict[str, Any]) -> str:
    """Stable deduplication key for (application, target_descriptor) pairs."""
    sorted_items = sorted(target_descriptor.items())
    descriptor_str = ','.join(f'{k}={v}' for k, v in sorted_items)
    return f'{application}::{descriptor_str}'


class GenerativePDPService:
    """Desired-state projection for a single subject snapshot.

    Stateless over input: all data is passed in; no DB access.

    Args:
        rule_pack: Pre-loaded RulePack.  Must contain ``birthright`` rules
                   (``rule.kind == 'birthright'``) to drive access generation.
                   Loaded once at service construction time.
    """

    def __init__(self, rule_pack: RulePack | None = None) -> None:
        self._rule_pack = rule_pack if rule_pack is not None else RulePack()

    def assess(
        self,
        subject_context: SubjectContext,
        current_facts: list[CurrentFact],
        current_initiatives: list[CurrentInitiative],
        context_overrides: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> list[ProjectedFact]:
        """Compute desired access state for the given subject snapshot.

        Args:
            subject_context: Full subject context (employee or NHI).
            current_facts: Current access facts from access_effective.
            current_initiatives: Current initiatives from inventory.initiatives.
            context_overrides: Optional attribute overrides for what-if analysis.
                               Applied on top of subject_context.attributes.
            now: Current time for validity checks.  Defaults to UTC now.

        Returns:
            List of ProjectedFact representing the desired access state.
            Deduplicated by (application, target_descriptor).
        """
        effective_now = now if now is not None else _now_utc()

        if context_overrides:
            merged_attrs = {**subject_context.attributes, **context_overrides}
            subject_context = subject_context.model_copy(update={'attributes': merged_attrs})

        # Map: dedupe_key -> ProjectedFact (accumulates initiatives)
        projected: dict[str, ProjectedFact] = {}

        self._apply_birthright(subject_context, projected, effective_now)
        self._apply_carry_over(subject_context, current_initiatives, projected, effective_now)

        return list(projected.values())

    def _apply_birthright(
        self,
        subject_context: SubjectContext,
        projected: dict[str, ProjectedFact],
        now: datetime,
    ) -> None:
        """Evaluate birthright rules and add generated facts to projected."""
        birthright_rules = [
            r
            for r in (self._rule_pack.lifecycle + self._rule_pack.risk + getattr(self._rule_pack, 'birthright', []))
            if r.kind == 'birthright'
        ]

        for rule in birthright_rules:
            if not _match_birthright_rule(rule, subject_context, now):
                continue

            application = str(rule.then.get('application', ''))
            if not application:
                continue
            target_descriptor: dict[str, Any] = dict(rule.then.get('target_descriptor', {}))
            fact_kind = str(rule.then.get('fact_kind', 'access'))

            key = _dedupe_key(application, target_descriptor)
            initiative = InitiativeProjection(
                type=InitiativeType.birthright,
                origin=f'policy_rule:{rule.id}',
                valid_from=None,
                valid_until=None,
                source_initiative_id=None,
            )

            if key in projected:
                projected[key].initiatives.append(initiative)
            else:
                decision = _build_birthright_decision(rule, subject_context)
                projected[key] = ProjectedFact(
                    fact_kind=fact_kind,
                    application=application,
                    target_descriptor=target_descriptor,
                    initiatives=[initiative],
                    decision=decision,
                )

    def _apply_carry_over(
        self,
        subject_context: SubjectContext,
        current_initiatives: list[CurrentInitiative],
        projected: dict[str, ProjectedFact],
        now: datetime,
    ) -> None:
        """Carry forward existing initiatives that are still valid."""
        if _employment_blocks_carry_over(subject_context):
            return

        for initiative in current_initiatives:
            if initiative.type not in _CARRY_OVER_TYPES:
                continue
            if not _is_active_initiative(initiative, now):
                continue

            key = _dedupe_key(initiative.application, initiative.target_descriptor)
            origin = _format_carry_over_origin(initiative)
            initiative_proj = InitiativeProjection(
                type=initiative.type,
                origin=origin,
                valid_from=initiative.valid_from,
                valid_until=initiative.valid_until,
                source_initiative_id=initiative.id,
            )

            if key in projected:
                projected[key].initiatives.append(initiative_proj)
            else:
                decision = _build_carry_over_decision(initiative, None)
                projected[key] = ProjectedFact(
                    fact_kind='access',
                    application=initiative.application,
                    target_descriptor=dict(initiative.target_descriptor),
                    initiatives=[initiative_proj],
                    decision=decision,
                )


def _format_carry_over_origin(initiative: CurrentInitiative) -> str:
    """Format the origin string for a carried-over initiative per C1 standard."""
    if initiative.type == InitiativeType.grace:
        return f'grace:{initiative.id}'
    if initiative.type == InitiativeType.delegated:
        return f'delegation:{initiative.origin}'
    if initiative.type == InitiativeType.requested:
        return f'request:{initiative.origin}'
    # For all other carry-over types, preserve the original origin
    return initiative.origin
