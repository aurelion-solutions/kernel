# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure unit tests for the EAS projector.  No DB, no events, no async."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from src.capabilities.effective_access.models import EffectiveGrantEffect
from src.capabilities.effective_access.projector import (
    AccessFactView,
    EffectiveGrantDraft,
    InitiativeView,
    project,
)
from src.inventory.access_facts.schemas import AccessFactEffect
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind

# ---------------------------------------------------------------------------
# Stable test constants
# ---------------------------------------------------------------------------

_FACT_ID = UUID('00000000-0000-0000-0000-000000000001')
_SUBJECT_ID = UUID('00000000-0000-0000-0000-000000000002')
_ACCOUNT_ID = UUID('00000000-0000-0000-0000-000000000003')
_APPLICATION_ID = UUID('00000000-0000-0000-0000-000000000004')
_RESOURCE_ID = UUID('00000000-0000-0000-0000-000000000005')
_INITIATIVE_ID = UUID('00000000-0000-0000-0000-000000000006')
_OTHER_FACT_ID = UUID('00000000-0000-0000-0000-000000000099')

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, tzinfo=UTC)
_T2 = datetime(2026, 12, 31, tzinfo=UTC)
_NOW = datetime(2026, 6, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------


def _make_fact(**overrides: object) -> AccessFactView:
    defaults: dict[str, object] = {
        'id': _FACT_ID,
        'subject_id': _SUBJECT_ID,
        'subject_kind': SubjectKind.employee,
        'account_id': None,
        'application_id': _APPLICATION_ID,
        'resource_id': _RESOURCE_ID,
        'action': Action.read,
        'effect': AccessFactEffect.allow,
        'valid_from': _T0,
        'valid_until': None,
    }
    defaults.update(overrides)
    return AccessFactView(**defaults)  # type: ignore[arg-type]


def _make_initiative(**overrides: object) -> InitiativeView:
    defaults: dict[str, object] = {
        'id': _INITIATIVE_ID,
        'access_fact_id': _FACT_ID,
        'type': InitiativeType.birthright,
        'origin': 'policy:test',
        'valid_from': _T0,
        'valid_until': None,
    }
    defaults.update(overrides)
    return InitiativeView(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Test 1: happy path — allow, open-ended window
# ---------------------------------------------------------------------------


def test_happy_path_allow_open_ended() -> None:
    fact = _make_fact()
    init = _make_initiative()
    result = project(fact, init, now=_NOW)

    assert len(result) == 1
    draft = result[0]
    assert isinstance(draft, EffectiveGrantDraft)
    assert draft.subject_id == _SUBJECT_ID
    assert draft.subject_kind == SubjectKind.employee
    assert draft.application_id == _APPLICATION_ID
    assert draft.account_id is None
    assert draft.resource_id == _RESOURCE_ID
    assert draft.action == Action.read
    assert draft.effect == EffectiveGrantEffect.allow
    assert draft.initiative_type == InitiativeType.birthright
    assert draft.initiative_origin == 'policy:test'
    assert draft.valid_from == _T0
    assert draft.valid_until is None
    assert draft.source_access_fact_id == _FACT_ID
    assert draft.source_initiative_id == _INITIATIVE_ID
    assert draft.observed_at == _NOW
    assert draft.tombstoned_at is None


# ---------------------------------------------------------------------------
# Test 2: happy path — closed window, valid_from/valid_until intersection
# ---------------------------------------------------------------------------


def test_happy_path_allow_closed_window() -> None:
    fact = _make_fact(valid_from=_T0, valid_until=_T2)
    init = _make_initiative(valid_from=_T1, valid_until=_T2)
    result = project(fact, init, now=_NOW)
    draft = result[0]
    assert draft.valid_from == _T1  # max
    assert draft.valid_until == _T2  # min (equal)


# ---------------------------------------------------------------------------
# Test 3: effect mapping (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('input_effect', 'expected_effect'),
    [
        (AccessFactEffect.allow, EffectiveGrantEffect.allow),
        (AccessFactEffect.deny, EffectiveGrantEffect.deny),
    ],
)
def test_effect_mapping(
    input_effect: AccessFactEffect,
    expected_effect: EffectiveGrantEffect,
) -> None:
    fact = _make_fact(effect=input_effect)
    init = _make_initiative()
    result = project(fact, init, now=_NOW)
    assert result[0].effect == expected_effect


# ---------------------------------------------------------------------------
# Test 4: all 9 initiative types project uniformly (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('initiative_type', list(InitiativeType))
def test_all_nine_initiative_types_project_uniformly(initiative_type: InitiativeType) -> None:
    fact = _make_fact()
    init = _make_initiative(type=initiative_type)
    result = project(fact, init, now=_NOW)
    assert len(result) == 1
    assert result[0].initiative_type == initiative_type


# ---------------------------------------------------------------------------
# Test 5: valid_until intersection — null-aware min (parametrized)
# ---------------------------------------------------------------------------

_T_EARLY = datetime(2026, 3, 1, tzinfo=UTC)
_T_LATE = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ('fact_until', 'init_until', 'expected_until'),
    [
        (None, None, None),
        (_T_EARLY, None, _T_EARLY),
        (None, _T_EARLY, _T_EARLY),
        (_T_EARLY, _T_LATE, _T_EARLY),
        (_T_LATE, _T_EARLY, _T_EARLY),
    ],
)
def test_valid_until_intersection(
    fact_until: datetime | None,
    init_until: datetime | None,
    expected_until: datetime | None,
) -> None:
    fact = _make_fact(valid_until=fact_until)
    init = _make_initiative(valid_until=init_until)
    result = project(fact, init, now=_NOW)
    assert result[0].valid_until == expected_until


# ---------------------------------------------------------------------------
# Test 6: valid_from intersection — always max (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('fact_from', 'init_from', 'expected_from'),
    [
        (_T_EARLY, _T_LATE, _T_LATE),
        (_T_LATE, _T_EARLY, _T_LATE),
        (_T0, _T0, _T0),
    ],
)
def test_valid_from_always_max(
    fact_from: datetime,
    init_from: datetime,
    expected_from: datetime,
) -> None:
    fact = _make_fact(valid_from=fact_from)
    init = _make_initiative(valid_from=init_from)
    result = project(fact, init, now=_NOW)
    assert result[0].valid_from == expected_from


# ---------------------------------------------------------------------------
# Test 7: birth-tombstone when window is empty
# ---------------------------------------------------------------------------


def test_birth_tombstone_when_window_empty() -> None:
    # initiative ends before fact starts → empty window after intersection
    fact_from = datetime(2026, 9, 1, tzinfo=UTC)
    init_until = datetime(2026, 3, 1, tzinfo=UTC)
    fact = _make_fact(valid_from=fact_from, valid_until=None)
    init = _make_initiative(valid_from=_T0, valid_until=init_until)
    result = project(fact, init, now=_NOW)
    draft = result[0]

    assert draft.tombstoned_at == _NOW
    # Raw values — not clamped
    assert draft.valid_from > draft.valid_until  # type: ignore[operator]
    assert draft.valid_from == fact_from
    assert draft.valid_until == init_until


# ---------------------------------------------------------------------------
# Test 8: idempotency — same inputs produce equal output
# ---------------------------------------------------------------------------


def test_idempotency_same_inputs_same_output() -> None:
    fact = _make_fact()
    init = _make_initiative()
    out1 = project(fact, init, now=_NOW)
    out2 = project(fact, init, now=_NOW)
    assert out1 == out2
    assert out1[0] == out2[0]


# ---------------------------------------------------------------------------
# Test 9: determinism — only observed_at (and tombstoned_at for empty window) varies with now
# ---------------------------------------------------------------------------


def test_determinism_only_observed_at_varies_with_now() -> None:
    _NOW2 = datetime(2030, 5, 5, 12, 0, tzinfo=UTC)
    fact = _make_fact()
    init = _make_initiative()

    # Non-empty window: only observed_at differs
    d1 = project(fact, init, now=_NOW)[0]
    d2 = project(fact, init, now=_NOW2)[0]
    assert d1.observed_at != d2.observed_at
    assert d1.tombstoned_at is None and d2.tombstoned_at is None
    assert d1.model_copy(update={'observed_at': d2.observed_at}) == d2

    # Empty window: observed_at AND tombstoned_at vary
    fact_empty = _make_fact(valid_from=datetime(2026, 9, 1, tzinfo=UTC))
    init_empty = _make_initiative(valid_until=datetime(2026, 3, 1, tzinfo=UTC))
    e1 = project(fact_empty, init_empty, now=_NOW)[0]
    e2 = project(fact_empty, init_empty, now=_NOW2)[0]
    assert e1.observed_at != e2.observed_at
    assert e1.tombstoned_at != e2.tombstoned_at
    assert e1.model_copy(update={'observed_at': e2.observed_at, 'tombstoned_at': e2.tombstoned_at}) == e2


# ---------------------------------------------------------------------------
# Test 10: pair mismatch raises ValueError
# ---------------------------------------------------------------------------


def test_pair_mismatch_raises_value_error() -> None:
    fact = _make_fact(id=_FACT_ID)
    init = _make_initiative(access_fact_id=_OTHER_FACT_ID)
    with pytest.raises(ValueError, match='initiative does not belong'):
        project(fact, init, now=_NOW)


# ---------------------------------------------------------------------------
# Test 11: account_id=None passes through
# ---------------------------------------------------------------------------


def test_account_id_none_passes_through() -> None:
    fact = _make_fact(account_id=None)
    init = _make_initiative()
    result = project(fact, init, now=_NOW)
    assert result[0].account_id is None


# ---------------------------------------------------------------------------
# Test 12: subject_kind passes through (parametrized)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('kind', list(SubjectKind))
def test_subject_kind_passes_through(kind: SubjectKind) -> None:
    fact = _make_fact(subject_kind=kind)
    init = _make_initiative()
    result = project(fact, init, now=_NOW)
    assert result[0].subject_kind == kind


# ---------------------------------------------------------------------------
# Test 13: no UUID minting — source IDs are byte-equal across calls
# ---------------------------------------------------------------------------


def test_no_uuid_minting() -> None:
    fact = _make_fact()
    init = _make_initiative()
    d1 = project(fact, init, now=_NOW)[0]
    d2 = project(fact, init, now=_NOW)[0]
    assert d1.source_access_fact_id == d2.source_access_fact_id == _FACT_ID
    assert d1.source_initiative_id == d2.source_initiative_id == _INITIATIVE_ID


# ---------------------------------------------------------------------------
# Test 14: initiative_origin passes through (unicode, long string)
# ---------------------------------------------------------------------------


def test_initiative_origin_passes_through() -> None:
    long_origin = 'policy:тест-длинная-строка-' + ('x' * 200)
    init = _make_initiative(origin=long_origin)
    result = project(_make_fact(), init, now=_NOW)
    assert result[0].initiative_origin == long_origin


# ---------------------------------------------------------------------------
# Test 15: observed_at is set from the now argument exactly
# ---------------------------------------------------------------------------


def test_observed_at_set_from_now_argument() -> None:
    distinctive_now = datetime(2030, 5, 5, 12, 0, 0, tzinfo=UTC)
    result = project(_make_fact(), _make_initiative(), now=distinctive_now)
    assert result[0].observed_at == distinctive_now
