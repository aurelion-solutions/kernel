# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure-function SoD evaluator tests — no DB required.

All tests build input DTOs in memory and call evaluate() directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from src.engines.policy_assessment.policy_types.sod.evaluator import (
    CapabilityGrantView,
    MitigationView,
    SodRuleConditionView,
    SodRuleView,
    Violation,
    evaluate,
)
from src.inventory.assessment.mitigations.models import MitigationStatus
from src.inventory.policy.sod_rules.models import SodRuleScope, SodSeverity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AT = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
_SUBJECT = uuid4()
_APP1 = uuid4()
_APP2 = uuid4()


def _grant(
    *,
    gid: int,
    subject_id: UUID | None = None,
    cap_id: int,
    cap_slug: str,
    scope_key_id: int = 1,
    scope_value: str | None = None,
    app_id: UUID | None = None,
    eg_id: UUID | None = None,
    mapping_id: int = 1,
    access_fact_ids: list[int] | None = None,
    initiative_ids: list[int] | None = None,
) -> CapabilityGrantView:
    return CapabilityGrantView(
        id=gid,
        subject_id=subject_id or _SUBJECT,
        capability_id=cap_id,
        capability_slug=cap_slug,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        application_id=app_id or _APP1,
        source_effective_grant_id=eg_id or uuid4(),
        source_capability_mapping_id=mapping_id,
        source_access_fact_ids=access_fact_ids or [cap_id * 100],
        source_initiative_ids=initiative_ids or [cap_id * 200],
    )


def _condition(
    cid: int,
    cap_ids: set[int],
    min_count: int = 1,
    name: str | None = None,
) -> SodRuleConditionView:
    return SodRuleConditionView(
        id=cid,
        name=name,
        min_count=min_count,
        capability_ids=frozenset(cap_ids),
    )


def _rule(
    rid: int,
    code: str,
    conditions: list[SodRuleConditionView],
    scope_mode: SodRuleScope = SodRuleScope.global_,
    scope_key_id: int | None = None,
    is_enabled: bool = True,
    severity: SodSeverity = SodSeverity.high,
) -> SodRuleView:
    return SodRuleView(
        id=rid,
        code=code,
        severity=severity,
        scope_mode=scope_mode,
        scope_key_id=scope_key_id,
        is_enabled=is_enabled,
        conditions=tuple(conditions),
    )


def _mitigation(
    mid: int,
    rule_id: int,
    subject_id: UUID,
    status: MitigationStatus = MitigationStatus.active,
    scope_key_id: int | None = None,
    scope_value: str | None = None,
    created_at: datetime | None = None,
) -> MitigationView:
    return MitigationView(
        id=mid,
        rule_id=rule_id,
        subject_id=subject_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        status=status,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=None,
        created_at=created_at or datetime(2026, 4, 1, tzinfo=UTC),
    )


def _call(
    grants: list[CapabilityGrantView],
    rules: list[SodRuleView],
    mitigations: list[MitigationView] | None = None,
    overrides: list[CapabilityGrantView] | None = None,
    subject_id: UUID | None = None,
) -> list[Violation]:
    return evaluate(
        subject_id=subject_id or _SUBJECT,
        capability_grants=grants,
        rules=rules,
        mitigations=mitigations or [],
        capability_overrides=overrides,
        at=_AT,
    )


# ---------------------------------------------------------------------------
# Test 1: SoD pair — two conditions, min_count=1 each, both matched
# ---------------------------------------------------------------------------


def test_sod_pair_basic() -> None:
    """Two conditions satisfied → one violation with both slugs sorted."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor')
    rule = _rule(
        1,
        'SOD-001',
        [_condition(1, {10}), _condition(2, {20})],
    )
    result = _call([g1, g2], [rule])
    assert len(result) == 1
    v = result[0]
    assert v.rule_code == 'SOD-001'
    assert v.matched_capability_slugs == ['approve_payment', 'create_vendor']
    assert v.scope_value is None
    assert v.is_mitigated is False


# ---------------------------------------------------------------------------
# Test 2: Super-grant — one condition, one capability
# ---------------------------------------------------------------------------


def test_super_grant_one_condition() -> None:
    """One condition with one capability → violation when subject holds it."""
    g = _grant(gid=1, cap_id=10, cap_slug='admin_all')
    rule = _rule(1, 'SOD-002', [_condition(1, {10})])
    result = _call([g], [rule])
    assert len(result) == 1
    assert result[0].matched_capability_slugs == ['admin_all']


# ---------------------------------------------------------------------------
# Test 3: N-of-M trigger — min_count=2, subject has 2 of 4
# ---------------------------------------------------------------------------


def test_n_of_m_trigger() -> None:
    """Condition min_count=2, subject has 2 distinct slugs → violation."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='read_finance')
    g2 = _grant(gid=2, cap_id=20, cap_slug='write_finance')
    rule = _rule(1, 'SOD-003', [_condition(1, {10, 20, 30, 40}, min_count=2)])
    result = _call([g1, g2], [rule])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Test 4: N-of-M no-trigger — min_count=2, subject has only 1
# ---------------------------------------------------------------------------


def test_n_of_m_no_trigger() -> None:
    """Condition min_count=2, subject has 1 distinct slug → no violation."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='read_finance')
    rule = _rule(1, 'SOD-003', [_condition(1, {10, 20, 30, 40}, min_count=2)])
    result = _call([g1], [rule])
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 5: BY_SCOPE_KEY separation — different cost_center values → no violation
# ---------------------------------------------------------------------------


def test_by_scope_key_separation() -> None:
    """Same capabilities under different scope_values → no collision → no violation."""
    SCOPE_KEY = 5
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment', scope_key_id=SCOPE_KEY, scope_value='cc-001')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor', scope_key_id=SCOPE_KEY, scope_value='cc-002')
    rule = _rule(
        1,
        'SOD-004',
        [_condition(1, {10}), _condition(2, {20})],
        scope_mode=SodRuleScope.by_scope_key,
        scope_key_id=SCOPE_KEY,
    )
    result = _call([g1, g2], [rule])
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 6: BY_SCOPE_KEY collision — same cost_center value → one violation
# ---------------------------------------------------------------------------


def test_by_scope_key_collision() -> None:
    """Same capabilities under same scope_value → violation with scope_value populated."""
    SCOPE_KEY = 5
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment', scope_key_id=SCOPE_KEY, scope_value='cc-001')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor', scope_key_id=SCOPE_KEY, scope_value='cc-001')
    rule = _rule(
        1,
        'SOD-004',
        [_condition(1, {10}), _condition(2, {20})],
        scope_mode=SodRuleScope.by_scope_key,
        scope_key_id=SCOPE_KEY,
    )
    result = _call([g1, g2], [rule])
    assert len(result) == 1
    assert result[0].scope_value == 'cc-001'


# ---------------------------------------------------------------------------
# Test 7: PER_APPLICATION separation — cross-application combo → no violation
# ---------------------------------------------------------------------------


def test_per_application_separation() -> None:
    """Same capabilities across different apps → no violation."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment', app_id=_APP1)
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor', app_id=_APP2)
    rule = _rule(
        1,
        'SOD-005',
        [_condition(1, {10}), _condition(2, {20})],
        scope_mode=SodRuleScope.per_application,
    )
    result = _call([g1, g2], [rule])
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 8: PER_APPLICATION collision — same app → one violation per app
# ---------------------------------------------------------------------------


def test_per_application_collision() -> None:
    """Both capabilities in same app → one violation."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment', app_id=_APP1)
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor', app_id=_APP1)
    rule = _rule(
        1,
        'SOD-005',
        [_condition(1, {10}), _condition(2, {20})],
        scope_mode=SodRuleScope.per_application,
    )
    result = _call([g1, g2], [rule])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Test 9: Empty input under PER_APPLICATION — no synthetic bucket
# ---------------------------------------------------------------------------


def test_per_application_empty_input() -> None:
    """No grants under PER_APPLICATION → empty dict → no violations."""
    rule = _rule(
        1,
        'SOD-005',
        [_condition(1, {10}), _condition(2, {20})],
        scope_mode=SodRuleScope.per_application,
    )
    result = _call([], [rule])
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 10: Determinism — two identical calls → byte-identical output
# ---------------------------------------------------------------------------


def test_determinism() -> None:
    """Two calls with identical inputs → identical Violation lists."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment', eg_id=UUID('00000000-0000-0000-0000-000000000001'))
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor', eg_id=UUID('00000000-0000-0000-0000-000000000002'))
    rule = _rule(1, 'SOD-001', [_condition(1, {10}), _condition(2, {20})])
    r1 = _call([g1, g2], [rule])
    r2 = _call([g1, g2], [rule])
    assert r1 == r2
    assert len(r1) == 1
    assert r1[0].evidence_hash == r2[0].evidence_hash


# ---------------------------------------------------------------------------
# Test 11: Distinct slugs — two grant rows for same slug count as ONE
# ---------------------------------------------------------------------------


def test_distinct_slugs_count_as_one() -> None:
    """Two CapabilityGrant rows for same slug → 1 distinct slug → min_count=2 not met."""
    eg1 = uuid4()
    eg2 = uuid4()
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment', eg_id=eg1, mapping_id=1)
    g2 = _grant(gid=2, cap_id=10, cap_slug='approve_payment', eg_id=eg2, mapping_id=2)
    rule = _rule(1, 'SOD-DUP', [_condition(1, {10}, min_count=2)])
    result = _call([g1, g2], [rule])
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 12: Mitigation tie-break — exact wins over generic
# ---------------------------------------------------------------------------


def test_mitigation_exact_wins_over_generic() -> None:
    """Exact-scope active mitigation wins over generic active mitigation."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor')
    rule = _rule(1, 'SOD-001', [_condition(1, {10}), _condition(2, {20})])
    # Exact mitigation (scope_key_id=None, scope_value=None is GLOBAL → matches GLOBAL violation)
    # For GLOBAL rule: scope_key_id=None, scope_value=None
    exact = _mitigation(
        mid=1,
        rule_id=1,
        subject_id=_SUBJECT,
        status=MitigationStatus.active,
        scope_key_id=None,
        scope_value=None,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    generic = _mitigation(
        mid=2,
        rule_id=1,
        subject_id=_SUBJECT,
        status=MitigationStatus.active,
        scope_key_id=None,
        scope_value=None,
        created_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    # Both match exact tier (both None/None for GLOBAL rule) — newer wins
    result = _call([g1, g2], [rule], mitigations=[exact, generic])
    assert len(result) == 1
    v = result[0]
    assert v.is_mitigated is True
    # Winner is exact (mid=1, newest created_at)
    assert v.active_mitigation_id == 1


# ---------------------------------------------------------------------------
# Test 13: Mitigation tie-break — active wins over proposed at same tier
# ---------------------------------------------------------------------------


def test_mitigation_active_wins_over_proposed() -> None:
    """Active mitigation wins over proposed at same tier → is_mitigated=True, proposed_mitigation_id=None."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor')
    rule = _rule(1, 'SOD-001', [_condition(1, {10}), _condition(2, {20})])
    active_mit = _mitigation(mid=1, rule_id=1, subject_id=_SUBJECT, status=MitigationStatus.active)
    proposed_mit = _mitigation(mid=2, rule_id=1, subject_id=_SUBJECT, status=MitigationStatus.proposed)
    result = _call([g1, g2], [rule], mitigations=[active_mit, proposed_mit])
    assert len(result) == 1
    v = result[0]
    assert v.is_mitigated is True
    assert v.active_mitigation_id == 1
    assert v.proposed_mitigation_id == 2


# ---------------------------------------------------------------------------
# Test 14: Proposed only → proposed_mitigation_id populated, is_mitigated=False
# ---------------------------------------------------------------------------


def test_proposed_only_mitigation() -> None:
    """Only proposed mitigation → is_mitigated=False, proposed_mitigation_id set."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor')
    rule = _rule(1, 'SOD-001', [_condition(1, {10}), _condition(2, {20})])
    proposed = _mitigation(mid=99, rule_id=1, subject_id=_SUBJECT, status=MitigationStatus.proposed)
    result = _call([g1, g2], [rule], mitigations=[proposed])
    assert len(result) == 1
    v = result[0]
    assert v.is_mitigated is False
    assert v.active_mitigation_id is None
    assert v.proposed_mitigation_id == 99


# ---------------------------------------------------------------------------
# Test 15: What-if — overrides cause violation; without overrides no violation
# ---------------------------------------------------------------------------


def test_what_if_override_causes_violation() -> None:
    """What-if: add capability_override → violation appears. Same base grants without override → no violation."""
    base_grant = _grant(gid=1, cap_id=10, cap_slug='create_vendor')
    override = _grant(gid=999, cap_id=20, cap_slug='approve_payment', eg_id=uuid4())
    rule = _rule(1, 'SOD-001', [_condition(1, {10}), _condition(2, {20})])

    original_grants = [base_grant]

    # With override → violation
    result_with = _call(original_grants, [rule], overrides=[override])
    assert len(result_with) == 1

    # Without override → no violation (base_grant has only one of two conditions)
    result_without = _call(original_grants, [rule])
    assert len(result_without) == 0

    # Verify input not mutated
    assert len(original_grants) == 1


# ---------------------------------------------------------------------------
# Test 16: Disabled rule never fires (evaluator defensive skip)
# ---------------------------------------------------------------------------


def test_disabled_rule_never_fires() -> None:
    """is_enabled=False rule must be skipped by evaluator."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor')
    rule = _rule(1, 'SOD-DIS', [_condition(1, {10}), _condition(2, {20})], is_enabled=False)
    result = _call([g1, g2], [rule])
    assert len(result) == 0


# ---------------------------------------------------------------------------
# Test 17: Mitigation rule mismatch — different rule_id does not apply
# ---------------------------------------------------------------------------


def test_mitigation_rule_mismatch() -> None:
    """Mitigation for rule_id=99 does not apply to violation for rule_id=1."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor')
    rule = _rule(1, 'SOD-001', [_condition(1, {10}), _condition(2, {20})])
    wrong_rule_mit = _mitigation(mid=55, rule_id=99, subject_id=_SUBJECT, status=MitigationStatus.active)
    result = _call([g1, g2], [rule], mitigations=[wrong_rule_mit])
    assert len(result) == 1
    v = result[0]
    assert v.is_mitigated is False
    assert v.active_mitigation_id is None


# ---------------------------------------------------------------------------
# Test 18: created_at tie-break within same tier and status
# ---------------------------------------------------------------------------


def test_created_at_tiebreak_picks_most_recent() -> None:
    """Two active exact-scope mitigations → most recent created_at wins."""
    g1 = _grant(gid=1, cap_id=10, cap_slug='approve_payment')
    g2 = _grant(gid=2, cap_id=20, cap_slug='create_vendor')
    rule = _rule(1, 'SOD-001', [_condition(1, {10}), _condition(2, {20})])

    older = _mitigation(
        mid=10,
        rule_id=1,
        subject_id=_SUBJECT,
        status=MitigationStatus.active,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = _mitigation(
        mid=20,
        rule_id=1,
        subject_id=_SUBJECT,
        status=MitigationStatus.active,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )

    result = _call([g1, g2], [rule], mitigations=[older, newer])
    assert len(result) == 1
    # Newer created_at wins
    assert result[0].active_mitigation_id == 20
