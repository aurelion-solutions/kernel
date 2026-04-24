# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure-function tests for detect_orphans — no DB, no IO."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from src.capabilities.access_analysis.detectors.orphan import (
    DEFAULT_ORPHAN_SEVERITY,
    AccountView,
    OrphanFinding,
    detect_orphans,
)
from src.capabilities.access_analysis.sod_rules.models import SodSeverity

_AT = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _make_account(
    *,
    subject_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
    username: str = 'alice',
    last_known_owner_subject_id: uuid.UUID | None = None,
) -> AccountView:
    return AccountView(
        id=uuid.uuid4(),
        application_id=application_id or uuid.uuid4(),
        subject_id=subject_id,
        username=username,
        last_known_owner_subject_id=last_known_owner_subject_id,
    )


# ---------------------------------------------------------------------------
# Test 1: empty input → empty output
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    result = detect_orphans(accounts=[], at=_AT)
    assert result == []


# ---------------------------------------------------------------------------
# Test 2: one orphan account → one finding
# ---------------------------------------------------------------------------


def test_one_orphan_account_produces_one_finding() -> None:
    account = _make_account(subject_id=None, username='orphan_user')
    result = detect_orphans(accounts=[account], at=_AT)
    assert len(result) == 1
    finding = result[0]
    assert isinstance(finding, OrphanFinding)
    assert finding.account_id == account.id
    assert finding.application_id == account.application_id
    assert finding.username == 'orphan_user'


# ---------------------------------------------------------------------------
# Test 3: one owned account → no finding
# ---------------------------------------------------------------------------


def test_owned_account_produces_no_finding() -> None:
    account = _make_account(subject_id=uuid.uuid4())
    result = detect_orphans(accounts=[account], at=_AT)
    assert result == []


# ---------------------------------------------------------------------------
# Test 4: mixed list → only orphan findings
# ---------------------------------------------------------------------------


def test_mixed_list_returns_only_orphans() -> None:
    orphan1 = _make_account(subject_id=None, username='orphan1')
    orphan2 = _make_account(subject_id=None, username='orphan2')
    orphan3 = _make_account(subject_id=None, username='orphan3')
    owned1 = _make_account(subject_id=uuid.uuid4(), username='owned1')
    owned2 = _make_account(subject_id=uuid.uuid4(), username='owned2')

    result = detect_orphans(accounts=[orphan1, owned1, orphan2, owned2, orphan3], at=_AT)
    assert len(result) == 3
    returned_ids = {f.account_id for f in result}
    assert orphan1.id in returned_ids
    assert orphan2.id in returned_ids
    assert orphan3.id in returned_ids
    assert owned1.id not in returned_ids
    assert owned2.id not in returned_ids


# ---------------------------------------------------------------------------
# Test 5: determinism — same input called twice → identical list
# ---------------------------------------------------------------------------


def test_determinism_same_input_identical_output() -> None:
    accounts = [_make_account(subject_id=None, username=f'user{i}') for i in range(5)]
    result1 = detect_orphans(accounts=accounts, at=_AT)
    result2 = detect_orphans(accounts=accounts, at=_AT)
    assert result1 == result2


# ---------------------------------------------------------------------------
# Test 6: sort order across application_id / username / account_id
# ---------------------------------------------------------------------------


def test_sort_order_by_application_username_account_id() -> None:
    app_a = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000000')
    app_b = uuid.UUID('bbbbbbbb-0000-0000-0000-000000000000')

    # Deliberately create with IDs to verify deterministic sort
    acc1 = AccountView(
        id=uuid.UUID('00000000-0000-0000-0000-000000000003'),
        application_id=app_b,
        subject_id=None,
        username='zeta',
        last_known_owner_subject_id=None,
    )
    acc2 = AccountView(
        id=uuid.UUID('00000000-0000-0000-0000-000000000002'),
        application_id=app_a,
        subject_id=None,
        username='beta',
        last_known_owner_subject_id=None,
    )
    acc3 = AccountView(
        id=uuid.UUID('00000000-0000-0000-0000-000000000001'),
        application_id=app_a,
        subject_id=None,
        username='alpha',
        last_known_owner_subject_id=None,
    )

    # Input in non-sorted order
    result = detect_orphans(accounts=[acc1, acc2, acc3], at=_AT)

    # Expected sort: (str(app_a), 'alpha', ...) < (str(app_a), 'beta', ...) < (str(app_b), 'zeta', ...)
    assert len(result) == 3
    assert result[0].account_id == acc3.id  # app_a, alpha
    assert result[1].account_id == acc2.id  # app_a, beta
    assert result[2].account_id == acc1.id  # app_b, zeta


# ---------------------------------------------------------------------------
# Test 7: severity is always SodSeverity.high
# ---------------------------------------------------------------------------


def test_severity_is_always_high() -> None:
    accounts = [_make_account(subject_id=None) for _ in range(3)]
    result = detect_orphans(accounts=accounts, at=_AT)
    for finding in result:
        assert finding.severity == SodSeverity.high
        assert finding.severity == DEFAULT_ORPHAN_SEVERITY


# ---------------------------------------------------------------------------
# Test 8: at parameter propagates verbatim into detected_at
# ---------------------------------------------------------------------------


def test_at_parameter_propagates_to_detected_at() -> None:
    custom_at = datetime(2025, 1, 15, 8, 30, 0, tzinfo=UTC)
    account = _make_account(subject_id=None)
    result = detect_orphans(accounts=[account], at=custom_at)
    assert len(result) == 1
    assert result[0].detected_at == custom_at


# ---------------------------------------------------------------------------
# Test 9: last_known_owner_subject_id propagates from input to output
# ---------------------------------------------------------------------------


def test_last_known_owner_subject_id_propagates_none() -> None:
    account = _make_account(subject_id=None, last_known_owner_subject_id=None)
    result = detect_orphans(accounts=[account], at=_AT)
    assert result[0].last_known_owner_subject_id is None


def test_last_known_owner_subject_id_propagates_some() -> None:
    owner_id = uuid.uuid4()
    account = _make_account(subject_id=None, last_known_owner_subject_id=owner_id)
    result = detect_orphans(accounts=[account], at=_AT)
    assert result[0].last_known_owner_subject_id == owner_id


# ---------------------------------------------------------------------------
# Test 10: sort tiebreak by account_id when application_id and username equal
# ---------------------------------------------------------------------------


def test_sort_tiebreak_by_account_id() -> None:
    app_id = uuid.uuid4()
    acc_lo = AccountView(
        id=uuid.UUID('00000000-0000-0000-0000-000000000001'),
        application_id=app_id,
        subject_id=None,
        username='same',
        last_known_owner_subject_id=None,
    )
    acc_hi = AccountView(
        id=uuid.UUID('ffffffff-ffff-ffff-ffff-ffffffffffff'),
        application_id=app_id,
        subject_id=None,
        username='same',
        last_known_owner_subject_id=None,
    )
    result = detect_orphans(accounts=[acc_hi, acc_lo], at=_AT)
    assert result[0].account_id == acc_lo.id
    assert result[1].account_id == acc_hi.id
