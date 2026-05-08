# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure-function tests for access_risk evaluators (orphaned_access + unused_access rules)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
import random
import uuid
from uuid import UUID, uuid4

from pydantic import ValidationError
import pytest
from src.engines.policy_assessment.policy_types.access_risk.evaluator import (
    DEFAULT_ORPHAN_SEVERITY,
    DEFAULT_UNUSED_SEVERITY,
    DEFAULT_UNUSED_THRESHOLD_DAYS,
    AccessFactView,
    AccountView,
    OrphanFinding,
    UnusedFinding,
    detect_orphans,
    detect_unused,
)
from src.inventory.policy.sod_rules.models import SodSeverity

_AT = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# orphaned_access rule helpers
# ---------------------------------------------------------------------------


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
# orphaned_access rule tests
# ---------------------------------------------------------------------------


def test_orphan_empty_input_returns_empty() -> None:
    assert detect_orphans(accounts=[], at=_AT) == []


def test_orphan_one_orphan_produces_one_finding() -> None:
    account = _make_account(subject_id=None, username='orphan_user')
    result = detect_orphans(accounts=[account], at=_AT)
    assert len(result) == 1
    f = result[0]
    assert isinstance(f, OrphanFinding)
    assert f.account_id == account.id
    assert f.username == 'orphan_user'


def test_orphan_owned_account_no_finding() -> None:
    assert detect_orphans(accounts=[_make_account(subject_id=uuid.uuid4())], at=_AT) == []


def test_orphan_mixed_list_returns_only_orphans() -> None:
    orphan1 = _make_account(subject_id=None, username='orphan1')
    orphan2 = _make_account(subject_id=None, username='orphan2')
    owned = _make_account(subject_id=uuid.uuid4(), username='owned')
    result = detect_orphans(accounts=[orphan1, owned, orphan2], at=_AT)
    assert len(result) == 2
    ids = {f.account_id for f in result}
    assert orphan1.id in ids and orphan2.id in ids and owned.id not in ids


def test_orphan_determinism() -> None:
    accounts = [_make_account(subject_id=None, username=f'u{i}') for i in range(5)]
    assert detect_orphans(accounts=accounts, at=_AT) == detect_orphans(accounts=accounts, at=_AT)


def test_orphan_sort_order() -> None:
    app_a = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000000')
    app_b = uuid.UUID('bbbbbbbb-0000-0000-0000-000000000000')
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
    result = detect_orphans(accounts=[acc1, acc2, acc3], at=_AT)
    assert result[0].account_id == acc3.id
    assert result[1].account_id == acc2.id
    assert result[2].account_id == acc1.id


def test_orphan_severity_always_high() -> None:
    result = detect_orphans(accounts=[_make_account(subject_id=None)], at=_AT)
    assert result[0].severity == SodSeverity.high == DEFAULT_ORPHAN_SEVERITY


def test_orphan_at_propagates_to_detected_at() -> None:
    custom_at = datetime(2025, 1, 15, 8, 30, 0, tzinfo=UTC)
    result = detect_orphans(accounts=[_make_account(subject_id=None)], at=custom_at)
    assert result[0].detected_at == custom_at


def test_orphan_last_known_owner_propagates() -> None:
    owner_id = uuid.uuid4()
    result = detect_orphans(accounts=[_make_account(subject_id=None, last_known_owner_subject_id=owner_id)], at=_AT)
    assert result[0].last_known_owner_subject_id == owner_id


# ---------------------------------------------------------------------------
# unused_access rule helpers
# ---------------------------------------------------------------------------


def _make_fact(
    *,
    last_seen: datetime | None,
    valid_from: datetime | None = None,
    id: UUID | None = None,
    subject_id: UUID | None = None,
    account_id: UUID | None = None,
    application_id: UUID | None = None,
) -> AccessFactView:
    return AccessFactView(
        id=id or uuid4(),
        subject_id=subject_id or uuid4(),
        account_id=account_id,
        resource_id=uuid4(),
        application_id=application_id or uuid4(),
        valid_from=valid_from or AT - timedelta(days=200),
        last_seen=last_seen,
    )


# ---------------------------------------------------------------------------
# unused_access rule tests
# ---------------------------------------------------------------------------


def test_unused_empty_input_returns_empty() -> None:
    assert detect_unused(access_facts=[], threshold_days=90, at=AT) == []


def test_unused_recent_usage_no_finding() -> None:
    assert detect_unused(access_facts=[_make_fact(last_seen=AT - timedelta(days=30))], threshold_days=90, at=AT) == []


def test_unused_exactly_at_threshold_is_finding() -> None:
    result = detect_unused(access_facts=[_make_fact(last_seen=AT - timedelta(days=90))], threshold_days=90, at=AT)
    assert len(result) == 1 and result[0].unused_for_days == 90


def test_unused_stale_usage_correct_days() -> None:
    result = detect_unused(access_facts=[_make_fact(last_seen=AT - timedelta(days=91))], threshold_days=90, at=AT)
    assert len(result) == 1 and result[0].unused_for_days == 91


def test_unused_no_usage_old_valid_from() -> None:
    fact = _make_fact(last_seen=None, valid_from=AT - timedelta(days=100))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert len(result) == 1 and result[0].unused_for_days == 100 and result[0].last_seen is None


def test_unused_no_usage_recent_valid_from_no_finding() -> None:
    fact = _make_fact(last_seen=None, valid_from=AT - timedelta(days=30))
    assert detect_unused(access_facts=[fact], threshold_days=90, at=AT) == []


def test_unused_partial_day_boundary_no_finding() -> None:
    fact = _make_fact(last_seen=AT - timedelta(days=89, hours=23))
    assert detect_unused(access_facts=[fact], threshold_days=90, at=AT) == []


def test_unused_severity_and_detected_at() -> None:
    result = detect_unused(access_facts=[_make_fact(last_seen=AT - timedelta(days=100))], threshold_days=90, at=AT)
    assert result[0].severity == SodSeverity.low == DEFAULT_UNUSED_SEVERITY
    assert result[0].detected_at == AT


def test_unused_mixed_list_three_findings() -> None:
    app_id = uuid4()
    facts = [
        _make_fact(last_seen=AT - timedelta(days=100), application_id=app_id),
        _make_fact(last_seen=AT - timedelta(days=91), application_id=app_id),
        _make_fact(last_seen=AT - timedelta(days=30), application_id=app_id),
        _make_fact(last_seen=None, valid_from=AT - timedelta(days=95), application_id=app_id),
        _make_fact(last_seen=None, valid_from=AT - timedelta(days=10), application_id=app_id),
    ]
    assert len(detect_unused(access_facts=facts, threshold_days=90, at=AT)) == 3


def test_unused_determinism_after_shuffle() -> None:
    app_id = uuid4()
    facts = [_make_fact(last_seen=AT - timedelta(days=100 + i), application_id=app_id) for i in range(5)]
    shuffled_a = facts[:]
    random.shuffle(shuffled_a)
    result_a = detect_unused(access_facts=shuffled_a, threshold_days=90, at=AT)
    shuffled_b = facts[:]
    random.shuffle(shuffled_b)
    result_b = detect_unused(access_facts=shuffled_b, threshold_days=90, at=AT)
    assert result_a == result_b
    keys = [(str(f.application_id), str(f.subject_id), str(f.access_fact_id)) for f in result_a]
    assert keys == sorted(keys)


def test_unused_finding_is_frozen() -> None:
    result = detect_unused(access_facts=[_make_fact(last_seen=AT - timedelta(days=100))], threshold_days=90, at=AT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result[0].unused_for_days = 999  # type: ignore[misc]


def test_unused_access_fact_view_accepts_none_fields() -> None:
    view = AccessFactView(
        id=uuid4(),
        subject_id=uuid4(),
        account_id=None,
        resource_id=uuid4(),
        application_id=uuid4(),
        valid_from=AT - timedelta(days=10),
        last_seen=None,
    )
    assert view.account_id is None and view.last_seen is None


def test_unused_access_fact_view_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        AccessFactView(  # type: ignore[call-arg]
            id=uuid4(),
            subject_id=uuid4(),
            account_id=None,
            resource_id=uuid4(),
            application_id=uuid4(),
            valid_from=AT - timedelta(days=10),
            last_seen=None,
            unknown_field='bad',
        )


def test_unused_module_constants() -> None:
    assert DEFAULT_UNUSED_SEVERITY == SodSeverity.low
    assert DEFAULT_UNUSED_THRESHOLD_DAYS == 90


def test_unused_finding_dataclass_is_frozen() -> None:
    result = detect_unused(access_facts=[_make_fact(last_seen=AT - timedelta(days=100))], threshold_days=90, at=AT)
    assert isinstance(result[0], UnusedFinding)
