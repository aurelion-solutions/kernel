# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure derivation function for Subject.status from principal state.

No DB access, no session, no side effects. Only ValueError on kind/type mismatch.
"""

from __future__ import annotations

from src.inventory.customers.models import Customer
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import NHI
from src.inventory.subjects.models import (
    SubjectCustomerStatus,
    SubjectEmployeeStatus,
    SubjectKind,
    SubjectNHIStatus,
)


def derive_subject_status_from_customer(customer: Customer) -> SubjectCustomerStatus:
    """Derive Subject.status for a customer principal.

    Rules:
    - is_locked=True  -> suspended
    - email_verified=True -> verified
    - else -> registered
    """
    if customer.is_locked:
        return SubjectCustomerStatus.suspended
    if customer.email_verified:
        return SubjectCustomerStatus.verified
    return SubjectCustomerStatus.registered


def derive_subject_status_from_employee(employee: Employee) -> SubjectEmployeeStatus:
    """Derive Subject.status for an employee principal.

    Rules:
    - is_locked=True -> on_leave
    - else -> active
    """
    if employee.is_locked:
        return SubjectEmployeeStatus.on_leave
    return SubjectEmployeeStatus.active


def derive_subject_status_from_nhi(nhi: NHI) -> SubjectNHIStatus:
    """Derive Subject.status for an NHI principal.

    Rules:
    - is_locked=True -> locked
    - else -> active
    """
    if nhi.is_locked:
        return SubjectNHIStatus.locked
    return SubjectNHIStatus.active


def derive_subject_status(kind: SubjectKind, principal: Customer | Employee | NHI) -> str:
    """Dispatch by kind to the per-kind derivation helper, returning the string value.

    Returns the `.value` of the kind-specific StrEnum member so callers get a plain
    str compatible with Subject.status (String(64)) without StrEnum comparison surprises.

    Raises ValueError when kind and principal type are inconsistent.
    """
    if kind == SubjectKind.customer:
        if not isinstance(principal, Customer):
            raise ValueError(f'kind={kind!r} but principal is {type(principal).__name__}; expected Customer')
        return derive_subject_status_from_customer(principal).value
    if kind == SubjectKind.employee:
        if not isinstance(principal, Employee):
            raise ValueError(f'kind={kind!r} but principal is {type(principal).__name__}; expected Employee')
        return derive_subject_status_from_employee(principal).value
    if kind == SubjectKind.nhi:
        if not isinstance(principal, NHI):
            raise ValueError(f'kind={kind!r} but principal is {type(principal).__name__}; expected NHI')
        return derive_subject_status_from_nhi(principal).value
    raise ValueError(f'Unknown SubjectKind: {kind!r}')
