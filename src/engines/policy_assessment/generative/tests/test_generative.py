# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for GenerativePDPService (C1 step).

Covers:
- Signature contract: correct input/output types.
- Birthright generation: rules with kind='birthright' produce ProjectedFact.
- Carry-over: requested/delegated/grace initiatives pass through.
- Carry-over filtering: expired valid_until excluded.
- Carry-over filtering: terminated employment blocks all carry-over.
- context_overrides: patches SubjectContext.attributes.
- Deduplication: same (application, target_descriptor) merges initiatives.
- Origin format: policy_rule:<id>, request:<origin>, delegation:<origin>, grace:<id>.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

from src.engines.policy_assessment.generative.schemas import (
    CurrentInitiative,
    ProjectedFact,
    SubjectContext,
)
from src.engines.policy_assessment.generative.service import GenerativePDPService
from src.engines.policy_assessment.schemas import Rule, RulePack
from src.inventory.initiatives.models import InitiativeType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC)
_FUTURE = _NOW + timedelta(days=30)
_PAST = _NOW - timedelta(days=1)


def _make_rule_pack(*birthright_rules: Rule) -> RulePack:
    return RulePack(birthright=list(birthright_rules))


def _birthright_rule(
    rule_id: str = 'br_github_dev',
    subject_kind: str = 'employee',
    subject_status: str = 'active',
    application: str = 'github',
    target_descriptor: dict | None = None,
) -> Rule:
    if target_descriptor is None:
        target_descriptor = {'role': 'developer'}
    return Rule(
        id=rule_id,
        kind='birthright',
        when={
            'subject.kind': subject_kind,
            'subject.status': subject_status,
        },
        then={
            'application': application,
            'target_descriptor': target_descriptor,
            'fact_kind': 'access',
        },
        precedence=10,
    )


def _employee_context(
    subject_ref: str = 'emp-001',
    status: str = 'active',
    **extra_attrs: str,
) -> SubjectContext:
    return SubjectContext(
        subject_ref=subject_ref,
        subject_type='employee',
        org_unit_id='eng',
        attributes={'status': status, **extra_attrs},
    )


def _initiative(
    initiative_type: InitiativeType,
    application: str = 'github',
    target_descriptor: dict | None = None,
    valid_until: datetime | None = None,
    valid_from: datetime | None = None,
    origin: str = 'req-42',
) -> CurrentInitiative:
    if target_descriptor is None:
        target_descriptor = {'role': 'developer'}
    return CurrentInitiative(
        id=uuid.uuid4(),
        access_fact_id=uuid.uuid4(),
        type=initiative_type,
        origin=origin,
        application=application,
        target_descriptor=target_descriptor,
        valid_until=valid_until,
        valid_from=valid_from,
    )


# ---------------------------------------------------------------------------
# Signature tests
# ---------------------------------------------------------------------------


def test_assess_returns_list_of_projected_facts() -> None:
    """assess() returns a list (possibly empty) of ProjectedFact."""
    svc = GenerativePDPService(RulePack())
    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[],
        now=_NOW,
    )
    assert isinstance(result, list)


def test_projected_fact_fields_present() -> None:
    """Each ProjectedFact has all required fields."""
    rule = _birthright_rule()
    svc = GenerativePDPService(_make_rule_pack(rule))

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[],
        now=_NOW,
    )

    assert len(result) == 1
    pf: ProjectedFact = result[0]
    assert isinstance(pf, ProjectedFact)
    assert pf.fact_kind == 'access'
    assert pf.application == 'github'
    assert isinstance(pf.target_descriptor, dict)
    assert len(pf.initiatives) == 1
    assert pf.decision is not None


# ---------------------------------------------------------------------------
# Birthright generation
# ---------------------------------------------------------------------------


def test_birthright_rule_match_generates_fact() -> None:
    """A matching birthright rule generates one ProjectedFact."""
    rule = _birthright_rule(rule_id='br_github_dev')
    svc = GenerativePDPService(_make_rule_pack(rule))

    result = svc.assess(
        subject_context=_employee_context(status='active'),
        current_facts=[],
        current_initiatives=[],
        now=_NOW,
    )

    assert len(result) == 1
    pf = result[0]
    assert pf.initiatives[0].type == InitiativeType.birthright
    assert pf.initiatives[0].origin == 'policy_rule:br_github_dev'


def test_birthright_rule_no_match_no_fact() -> None:
    """Birthright rule that doesn't match subject.status produces nothing."""
    rule = _birthright_rule(subject_status='active')
    svc = GenerativePDPService(_make_rule_pack(rule))

    result = svc.assess(
        subject_context=_employee_context(status='terminated'),
        current_facts=[],
        current_initiatives=[],
        now=_NOW,
    )

    assert result == []


def test_birthright_two_rules_same_subject_produces_two_facts() -> None:
    """Two matching rules for different applications produce two facts."""
    rule_github = _birthright_rule(rule_id='br_github', application='github')
    rule_jira = _birthright_rule(rule_id='br_jira', application='jira')
    svc = GenerativePDPService(_make_rule_pack(rule_github, rule_jira))

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[],
        now=_NOW,
    )

    apps = {pf.application for pf in result}
    assert apps == {'github', 'jira'}


def test_birthright_rule_wrong_kind_ignored() -> None:
    """Rules with kind='lifecycle' or kind='risk' are not processed as birthright."""
    lifecycle_rule = Rule(
        id='emp_active',
        kind='lifecycle',
        when={'subject.kind': 'employee', 'subject.status': 'active'},
        then={'abstract_state': 'enabled'},
        precedence=10,
    )
    svc = GenerativePDPService(RulePack(lifecycle=[lifecycle_rule]))

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[],
        now=_NOW,
    )

    assert result == []


# ---------------------------------------------------------------------------
# Carry-over filtering
# ---------------------------------------------------------------------------


def test_carry_over_requested_active() -> None:
    """Active requested initiative is carried over."""
    init = _initiative(
        InitiativeType.requested,
        valid_until=_FUTURE,
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert len(result) == 1
    assert result[0].initiatives[0].type == InitiativeType.requested
    assert result[0].initiatives[0].origin == f'request:{init.origin}'


def test_carry_over_delegated_active() -> None:
    """Active delegated initiative is carried over with delegation: prefix."""
    init = _initiative(
        InitiativeType.delegated,
        valid_until=_FUTURE,
        origin='emp-manager-001',
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert len(result) == 1
    assert result[0].initiatives[0].origin == 'delegation:emp-manager-001'


def test_carry_over_grace_active() -> None:
    """Active grace initiative is carried over with grace:<initiative_id> origin."""
    init = _initiative(
        InitiativeType.grace,
        valid_until=_FUTURE,
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert len(result) == 1
    assert result[0].initiatives[0].origin == f'grace:{init.id}'


def test_carry_over_expired_valid_until_excluded() -> None:
    """Initiative with valid_until in the past is NOT carried over."""
    init = _initiative(
        InitiativeType.requested,
        valid_until=_PAST,
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert result == []


def test_carry_over_future_valid_from_excluded() -> None:
    """Initiative with valid_from in the future is NOT carried over yet."""
    init = _initiative(
        InitiativeType.requested,
        valid_from=_FUTURE,
        valid_until=None,
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert result == []


def test_carry_over_terminated_employment_blocks_all() -> None:
    """Terminated employment status blocks all carry-over."""
    init = _initiative(
        InitiativeType.requested,
        valid_until=_FUTURE,
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(employment_status='terminated'),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert result == []


def test_carry_over_birthright_type_not_carried() -> None:
    """Birthright initiatives are NOT in the carry-over set (they are regenerated)."""
    init = _initiative(
        InitiativeType.birthright,
        valid_until=_FUTURE,
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert result == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_birthright_and_carry_over_same_fact_merges_initiatives() -> None:
    """Birthright rule + carry-over for same (app, descriptor) merge into one ProjectedFact."""
    rule = _birthright_rule(
        rule_id='br_github_dev',
        application='github',
        target_descriptor={'role': 'developer'},
    )
    init = _initiative(
        InitiativeType.requested,
        application='github',
        target_descriptor={'role': 'developer'},
        valid_until=_FUTURE,
    )
    svc = GenerativePDPService(_make_rule_pack(rule))

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert len(result) == 1
    types = {i.type for i in result[0].initiatives}
    assert InitiativeType.birthright in types
    assert InitiativeType.requested in types


# ---------------------------------------------------------------------------
# context_overrides
# ---------------------------------------------------------------------------


def test_context_overrides_patch_attributes() -> None:
    """context_overrides patches subject_context.attributes without mutation."""
    rule = _birthright_rule(subject_status='contractor')
    svc = GenerativePDPService(_make_rule_pack(rule))

    # Without override: status=active → rule doesn't match
    result_no_override = svc.assess(
        subject_context=_employee_context(status='active'),
        current_facts=[],
        current_initiatives=[],
        now=_NOW,
    )
    assert result_no_override == []

    # With override: status→contractor → rule matches
    result_with_override = svc.assess(
        subject_context=_employee_context(status='active'),
        current_facts=[],
        current_initiatives=[],
        context_overrides={'status': 'contractor'},
        now=_NOW,
    )
    assert len(result_with_override) == 1


def test_context_overrides_do_not_mutate_original() -> None:
    """context_overrides must not modify the passed-in SubjectContext."""
    ctx = _employee_context(status='active')
    rule = _birthright_rule(subject_status='contractor')
    svc = GenerativePDPService(_make_rule_pack(rule))

    svc.assess(
        subject_context=ctx,
        current_facts=[],
        current_initiatives=[],
        context_overrides={'status': 'contractor'},
        now=_NOW,
    )

    assert ctx.attributes['status'] == 'active'


# ---------------------------------------------------------------------------
# Carry-over open-ended (valid_until=None)
# ---------------------------------------------------------------------------


def test_carry_over_open_ended_initiative_included() -> None:
    """Initiative with valid_until=None (indefinite) is always carried over."""
    init = _initiative(
        InitiativeType.requested,
        valid_until=None,
    )
    svc = GenerativePDPService(RulePack())

    result = svc.assess(
        subject_context=_employee_context(),
        current_facts=[],
        current_initiatives=[init],
        now=_NOW,
    )

    assert len(result) == 1
