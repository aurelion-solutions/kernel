# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure-function tests for detect_terminated — no DB, no IO."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import random
import uuid

from pydantic import ValidationError
import pytest
from src.engines.policy_assessment.policy_types.lifecycle.evaluator import (
    DEFAULT_TERMINATED_SEVERITY,
    AccountWithSubjectView,
    TerminatedFinding,
    detect_terminated,
    is_terminal_status,
)
from src.inventory.policy.sod_rules.models import SodSeverity
from src.inventory.subjects.models import SubjectKind

_AT = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _make_account(
    *,
    subject_kind: SubjectKind = SubjectKind.employee,
    subject_status: str = 'active',
    application_id: uuid.UUID | None = None,
    username: str = 'alice',
    subject_external_id: str = 'ext-001',
) -> AccountWithSubjectView:
    return AccountWithSubjectView(
        id=uuid.uuid4(),
        application_id=application_id or uuid.uuid4(),
        subject_id=uuid.uuid4(),
        username=username,
        subject_kind=subject_kind,
        subject_status=subject_status,
        subject_external_id=subject_external_id,
    )


# ---------------------------------------------------------------------------
# Test 1: empty input → empty output
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    result = detect_terminated(accounts=[], at=_AT)
    assert result == []


# ---------------------------------------------------------------------------
# Test 2: active employee → no finding
# ---------------------------------------------------------------------------


def test_active_employee_no_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.employee, subject_status='active')
    result = detect_terminated(accounts=[account], at=_AT)
    assert result == []


# ---------------------------------------------------------------------------
# Test 3: terminated employee → one finding, severity high, detected_at == at
# ---------------------------------------------------------------------------


def test_terminated_employee_produces_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.employee, subject_status='terminated')
    result = detect_terminated(accounts=[account], at=_AT)
    assert len(result) == 1
    finding = result[0]
    assert isinstance(finding, TerminatedFinding)
    assert finding.account_id == account.id
    assert finding.severity == SodSeverity.high
    assert finding.detected_at == _AT
    assert finding.subject_kind == SubjectKind.employee
    assert finding.subject_status == 'terminated'


# ---------------------------------------------------------------------------
# Test 4: NHI expired → finding
# ---------------------------------------------------------------------------


def test_nhi_expired_produces_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.nhi, subject_status='expired')
    result = detect_terminated(accounts=[account], at=_AT)
    assert len(result) == 1
    assert result[0].subject_status == 'expired'


# ---------------------------------------------------------------------------
# Test 5: NHI locked → finding
# ---------------------------------------------------------------------------


def test_nhi_locked_produces_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.nhi, subject_status='locked')
    result = detect_terminated(accounts=[account], at=_AT)
    assert len(result) == 1
    assert result[0].subject_status == 'locked'


# ---------------------------------------------------------------------------
# Test 6: NHI active → no finding
# ---------------------------------------------------------------------------


def test_nhi_active_no_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.nhi, subject_status='active')
    result = detect_terminated(accounts=[account], at=_AT)
    assert result == []


# ---------------------------------------------------------------------------
# Test 7: customer banned → finding
# ---------------------------------------------------------------------------


def test_customer_banned_produces_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.customer, subject_status='banned')
    result = detect_terminated(accounts=[account], at=_AT)
    assert len(result) == 1
    assert result[0].subject_status == 'banned'


# ---------------------------------------------------------------------------
# Test 8: customer deletion_requested → finding
# ---------------------------------------------------------------------------


def test_customer_deletion_requested_produces_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.customer, subject_status='deletion_requested')
    result = detect_terminated(accounts=[account], at=_AT)
    assert len(result) == 1
    assert result[0].subject_status == 'deletion_requested'


# ---------------------------------------------------------------------------
# Test 9: customer active → no finding
# ---------------------------------------------------------------------------


def test_customer_active_no_finding() -> None:
    account = _make_account(subject_kind=SubjectKind.customer, subject_status='active')
    result = detect_terminated(accounts=[account], at=_AT)
    assert result == []


# ---------------------------------------------------------------------------
# Test 10: mixed list (3 terminal + 2 live across all three kinds) → 3 findings
# ---------------------------------------------------------------------------


def test_mixed_list_returns_only_terminal_findings() -> None:
    terminated_emp = _make_account(subject_kind=SubjectKind.employee, subject_status='terminated', username='emp_t')
    active_emp = _make_account(subject_kind=SubjectKind.employee, subject_status='active', username='emp_a')
    expired_nhi = _make_account(subject_kind=SubjectKind.nhi, subject_status='expired', username='nhi_e')
    active_nhi = _make_account(subject_kind=SubjectKind.nhi, subject_status='active', username='nhi_a')
    banned_cust = _make_account(subject_kind=SubjectKind.customer, subject_status='banned', username='cust_b')

    result = detect_terminated(
        accounts=[terminated_emp, active_emp, expired_nhi, active_nhi, banned_cust],
        at=_AT,
    )
    assert len(result) == 3
    returned_ids = {f.account_id for f in result}
    assert terminated_emp.id in returned_ids
    assert expired_nhi.id in returned_ids
    assert banned_cust.id in returned_ids
    assert active_emp.id not in returned_ids
    assert active_nhi.id not in returned_ids


# ---------------------------------------------------------------------------
# Test 11: determinism — shuffled input gives identical output
# ---------------------------------------------------------------------------


def test_determinism_shuffled_input_identical_output() -> None:
    app_id = uuid.uuid4()
    accounts = [
        _make_account(
            subject_kind=SubjectKind.employee,
            subject_status='terminated',
            application_id=app_id,
            username=f'user{i}',
        )
        for i in range(5)
    ]

    result1 = detect_terminated(accounts=list(accounts), at=_AT)

    shuffled = list(accounts)
    random.shuffle(shuffled)
    result2 = detect_terminated(accounts=shuffled, at=_AT)

    assert result1 == result2


# ---------------------------------------------------------------------------
# Test 12: is_terminal_status — cross-kind status does not leak
# ---------------------------------------------------------------------------


def test_is_terminal_status_truths() -> None:
    assert is_terminal_status(SubjectKind.employee, 'terminated') is True
    assert is_terminal_status(SubjectKind.employee, 'active') is False
    # 'banned' is customer-only — must not match nhi
    assert is_terminal_status(SubjectKind.nhi, 'banned') is False


# ---------------------------------------------------------------------------
# Test 13: TerminatedFinding is frozen
# ---------------------------------------------------------------------------


def test_terminated_finding_is_frozen() -> None:
    finding = TerminatedFinding(
        account_id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        username='alice',
        subject_id=uuid.uuid4(),
        subject_kind=SubjectKind.employee,
        subject_status='terminated',
        subject_external_id='ext-001',
        severity=DEFAULT_TERMINATED_SEVERITY,
        detected_at=_AT,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        finding.username = 'bob'  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 14: AccountWithSubjectView rejects subject_id=None
# ---------------------------------------------------------------------------


def test_account_with_subject_view_rejects_none_subject_id() -> None:
    with pytest.raises(ValidationError):
        AccountWithSubjectView(
            id=uuid.uuid4(),
            application_id=uuid.uuid4(),
            subject_id=None,  # type: ignore[arg-type]
            username='alice',
            subject_kind=SubjectKind.employee,
            subject_status='terminated',
            subject_external_id='ext-001',
        )
