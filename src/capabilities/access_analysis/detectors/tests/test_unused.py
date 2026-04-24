# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure-function tests for detect_unused — no DB, no network."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
import random
from uuid import UUID, uuid4

import pytest
from src.capabilities.access_analysis.detectors.unused import (
    DEFAULT_UNUSED_SEVERITY,
    DEFAULT_UNUSED_THRESHOLD_DAYS,
    AccessFactView,
    detect_unused,
)
from src.capabilities.access_analysis.sod_rules.models import SodSeverity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = UTC

AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)


def _make_fact(
    *,
    last_seen: datetime | None,
    valid_from: datetime | None = None,
    id: UUID | None = None,
    subject_id: UUID | None = None,
    account_id: UUID | None = None,
    application_id: UUID | None = None,
) -> AccessFactView:
    if valid_from is None:
        valid_from = AT - timedelta(days=200)
    return AccessFactView(
        id=id or uuid4(),
        subject_id=subject_id or uuid4(),
        account_id=account_id,
        resource_id=uuid4(),
        application_id=application_id or uuid4(),
        valid_from=valid_from,
        last_seen=last_seen,
    )


# ---------------------------------------------------------------------------
# T1: empty input → empty output
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_list() -> None:
    result = detect_unused(access_facts=[], threshold_days=90, at=AT)
    assert result == []


# ---------------------------------------------------------------------------
# T2: last_seen = at - 30 days, threshold 90 → no finding
# ---------------------------------------------------------------------------


def test_recent_usage_no_finding() -> None:
    fact = _make_fact(last_seen=AT - timedelta(days=30))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert result == []


# ---------------------------------------------------------------------------
# T3: last_seen = at - 90 days, threshold 90 → finding (boundary inclusive)
# ---------------------------------------------------------------------------


def test_exactly_at_threshold_is_finding() -> None:
    fact = _make_fact(last_seen=AT - timedelta(days=90))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert len(result) == 1
    assert result[0].unused_for_days == 90


# ---------------------------------------------------------------------------
# T4: last_seen = at - 91 days, threshold 90 → finding, unused_for_days == 91
# ---------------------------------------------------------------------------


def test_stale_usage_returns_finding_with_correct_days() -> None:
    fact = _make_fact(last_seen=AT - timedelta(days=91))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert len(result) == 1
    assert result[0].unused_for_days == 91


# ---------------------------------------------------------------------------
# T5: last_seen = None, valid_from = at - 100 days → finding, unused_for_days == 100
# ---------------------------------------------------------------------------


def test_no_usage_old_valid_from_returns_finding() -> None:
    fact = _make_fact(last_seen=None, valid_from=AT - timedelta(days=100))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert len(result) == 1
    assert result[0].unused_for_days == 100
    assert result[0].last_seen is None


# ---------------------------------------------------------------------------
# T6: last_seen = None, valid_from = at - 30 days → no finding (brand-new grant)
# ---------------------------------------------------------------------------


def test_no_usage_recent_valid_from_no_finding() -> None:
    fact = _make_fact(last_seen=None, valid_from=AT - timedelta(days=30))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert result == []


# ---------------------------------------------------------------------------
# T7: partial-day boundary — 89 days + 23 hours → no finding
# ---------------------------------------------------------------------------


def test_partial_day_boundary_no_finding() -> None:
    fact = _make_fact(last_seen=AT - timedelta(days=89, hours=23))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert result == []


# ---------------------------------------------------------------------------
# T8: severity == SodSeverity.low and detected_at == at on every finding
# ---------------------------------------------------------------------------


def test_severity_and_detected_at_on_findings() -> None:
    fact = _make_fact(last_seen=AT - timedelta(days=100))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert len(result) == 1
    assert result[0].severity == SodSeverity.low
    assert result[0].detected_at == AT


# ---------------------------------------------------------------------------
# T9: mixed list — 3 findings out of 5
# ---------------------------------------------------------------------------


def test_mixed_list_three_findings() -> None:
    app_id = uuid4()
    facts = [
        _make_fact(last_seen=AT - timedelta(days=100), application_id=app_id),  # old usage
        _make_fact(last_seen=AT - timedelta(days=91), application_id=app_id),  # old usage
        _make_fact(last_seen=AT - timedelta(days=30), application_id=app_id),  # recent, no finding
        _make_fact(last_seen=None, valid_from=AT - timedelta(days=95), application_id=app_id),
        _make_fact(last_seen=None, valid_from=AT - timedelta(days=10), application_id=app_id),
    ]
    result = detect_unused(access_facts=facts, threshold_days=90, at=AT)
    assert len(result) == 3


# ---------------------------------------------------------------------------
# T10: determinism — shuffle 5-row input twice, same result
# ---------------------------------------------------------------------------


def test_determinism_after_shuffle() -> None:
    app_id = uuid4()
    facts = [_make_fact(last_seen=AT - timedelta(days=100 + i), application_id=app_id) for i in range(5)]

    shuffled_a = facts[:]
    random.shuffle(shuffled_a)
    result_a = detect_unused(access_facts=shuffled_a, threshold_days=90, at=AT)

    shuffled_b = facts[:]
    random.shuffle(shuffled_b)
    result_b = detect_unused(access_facts=shuffled_b, threshold_days=90, at=AT)

    assert result_a == result_b
    # verify sort key
    keys = [(str(f.application_id), str(f.subject_id), str(f.access_fact_id)) for f in result_a]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# T11: UnusedFinding is frozen — assigning to field raises FrozenInstanceError
# ---------------------------------------------------------------------------


def test_unused_finding_is_frozen() -> None:
    fact = _make_fact(last_seen=AT - timedelta(days=100))
    result = detect_unused(access_facts=[fact], threshold_days=90, at=AT)
    assert len(result) == 1
    with pytest.raises(dataclasses.FrozenInstanceError):
        result[0].unused_for_days = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# T12: AccessFactView accepts last_seen=None and account_id=None
# ---------------------------------------------------------------------------


def test_access_fact_view_accepts_none_fields() -> None:
    view = AccessFactView(
        id=uuid4(),
        subject_id=uuid4(),
        account_id=None,
        resource_id=uuid4(),
        application_id=uuid4(),
        valid_from=AT - timedelta(days=10),
        last_seen=None,
    )
    assert view.account_id is None
    assert view.last_seen is None


# ---------------------------------------------------------------------------
# T13: AccessFactView rejects unknown fields (extra='forbid')
# ---------------------------------------------------------------------------


def test_access_fact_view_rejects_extra_fields() -> None:
    from pydantic import ValidationError

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


# ---------------------------------------------------------------------------
# T14: module constants have expected values
# ---------------------------------------------------------------------------


def test_module_constants() -> None:
    assert DEFAULT_UNUSED_SEVERITY == SodSeverity.low
    assert DEFAULT_UNUSED_THRESHOLD_DAYS == 90
