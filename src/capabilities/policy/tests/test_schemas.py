# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Validation tests for PDP schemas."""

from datetime import UTC, datetime, timedelta

from src.capabilities.policy.schemas import (
    AbstractState,
    Decision,
    Facts,
    Initiative,
    OwnerFacts,
    Reason,
    RiskLevel,
    Rule,
    RulePack,
    SubjectFacts,
    TargetFacts,
    ThreatFacts,
)


def _make_subject(**overrides: object) -> dict:
    base: dict = {'id': 'emp-1', 'kind': 'employee', 'status': 'active'}
    base.update(overrides)
    return base


def _make_target(**overrides: object) -> dict:
    base: dict = {'application': 'app-1'}
    base.update(overrides)
    return base


NOW = datetime(2026, 1, 1, tzinfo=UTC)


# --- 1. All models instantiate with valid data ---


def test_employee_subject():
    s = SubjectFacts(**_make_subject(org_unit='engineering', start_date=NOW))
    assert s.kind == 'employee'
    assert s.org_unit == 'engineering'


def test_nhi_subject():
    owner = OwnerFacts(id='emp-1', status='active')
    s = SubjectFacts(**_make_subject(kind='nhi', nhi_kind='service_account', owner=owner, expires_at=NOW))
    assert s.nhi_kind == 'service_account'
    assert s.owner is not None
    assert s.owner.id == 'emp-1'


def test_customer_subject():
    s = SubjectFacts(
        **_make_subject(
            kind='customer',
            email_verified=True,
            tenant_id='t-1',
            tenant_role='admin',
            tenant_status='active',
            plan_tier='enterprise',
        )
    )
    assert s.tenant_id == 't-1'
    assert s.plan_tier == 'enterprise'


# --- 2. Facts accepts target=None (IDP subject-level scenario) ---


def test_facts_target_none():
    f = Facts(subject=SubjectFacts(**_make_subject()), target=None, now=NOW)
    assert f.target is None


# --- 3. Facts accepts threat=None ---


def test_facts_threat_none():
    f = Facts(subject=SubjectFacts(**_make_subject()), now=NOW)
    assert f.threat is None


# --- 4. Initiative with valid_until in past / future ---


def test_initiative_past():
    past = NOW - timedelta(days=30)
    i = Initiative(type='sod', valid_until=past)
    assert i.valid_until is not None
    assert i.valid_until < NOW


def test_initiative_future():
    future = NOW + timedelta(days=30)
    i = Initiative(type='sod', valid_until=future)
    assert i.valid_until is not None
    assert i.valid_until > NOW


# --- 5. Decision with risk_level=None and with a set value ---


def test_decision_risk_level_none():
    d = Decision(abstract_state=AbstractState.enabled)
    assert d.risk_level is None


def test_decision_risk_level_set():
    d = Decision(abstract_state=AbstractState.enabled, risk_level=RiskLevel.high)
    assert d.risk_level == RiskLevel.high


# --- 6. Action as plain string and as dict ---


def test_decision_action_string():
    d = Decision(abstract_state=AbstractState.disabled, actions=['revoke'])
    assert d.actions == ['revoke']


def test_decision_action_dict():
    d = Decision(
        abstract_state=AbstractState.disabled,
        actions=[{'type': 'notify', 'channel': 'email'}],
    )
    assert d.actions[0] == {'type': 'notify', 'channel': 'email'}


# --- 7. Rule and RulePack basic validation ---


def test_rule_basic():
    r = Rule(
        id='r-1',
        kind='lifecycle',
        when={'subject.status': 'terminated'},
        then={'abstract_state': 'disabled'},
        precedence=100,
    )
    assert r.precedence == 100


def test_rulepack_basic():
    rule = Rule(
        id='r-1',
        kind='lifecycle',
        when={'subject.status': 'terminated'},
        then={'abstract_state': 'disabled'},
        precedence=100,
    )
    rp = RulePack(
        lifecycle=[rule],
        risk=[],
        mapping={'app-1': {'disabled': {'concrete': 'locked', 'actions': ['revoke']}}},
    )
    assert len(rp.lifecycle) == 1
    assert 'app-1' in rp.mapping


# --- 8. AbstractState and RiskLevel enum membership ---


def test_abstract_state_members():
    assert set(AbstractState) == {'enabled', 'suspended', 'disabled', 'pending', 'grace'}


def test_risk_level_members():
    assert set(RiskLevel) == {'critical', 'high', 'medium', 'low'}


# --- 9. SubjectFacts defaults ---


def test_subject_defaults():
    s = SubjectFacts(**_make_subject())
    assert s.mfa_enabled is True
    assert s.required_consents_met is True


# --- 10. TargetFacts defaults ---


def test_target_defaults():
    t = TargetFacts(**_make_target())
    assert t.has_pending_attestation is False
    assert t.pending_reattestation is False


# --- 11. Serialization round-trip ---


def test_facts_round_trip():
    facts = Facts(
        subject=SubjectFacts(**_make_subject()),
        target=TargetFacts(**_make_target()),
        threat=ThreatFacts(risk_score=0.8, active_indicators=['tor_exit']),
        now=NOW,
    )
    dumped = facts.model_dump()
    restored = Facts(**dumped)
    assert restored == facts


def test_decision_round_trip():
    reason = Reason(
        rule_id='r-1',
        rule_kind='lifecycle',
        precedence=100,
        matched_conditions={'subject.status': 'terminated'},
        fact_values={'subject.status': 'terminated'},
        produced={'abstract_state': 'disabled'},
    )
    decision = Decision(
        abstract_state=AbstractState.disabled,
        concrete_state='locked',
        risk_level=RiskLevel.critical,
        actions=['revoke', {'type': 'notify', 'channel': 'slack'}],
        signals=['account_terminated'],
        reasons=[reason],
    )
    dumped = decision.model_dump()
    restored = Decision(**dumped)
    assert restored == decision
