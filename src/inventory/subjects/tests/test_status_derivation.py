# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure unit tests for Subject.status derivation function.

No DB, no session — plain model objects, not persisted.
"""

from __future__ import annotations

import pytest
from src.inventory.customers.models import Customer
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import NHI
from src.inventory.subjects.models import (
    SubjectCustomerStatus,
    SubjectEmployeeStatus,
    SubjectKind,
    SubjectNHIStatus,
)
from src.inventory.subjects.status_derivation import (
    derive_subject_status,
    derive_subject_status_from_customer,
    derive_subject_status_from_employee,
    derive_subject_status_from_nhi,
)

# ---------------------------------------------------------------------------
# Customer derivation
# ---------------------------------------------------------------------------


def test_derive_customer_suspended_when_locked() -> None:
    """is_locked=True on Customer -> SubjectCustomerStatus.suspended."""
    customer = Customer(is_locked=True, email_verified=False)
    result = derive_subject_status_from_customer(customer)
    assert result == SubjectCustomerStatus.suspended


def test_derive_customer_suspended_when_locked_and_verified() -> None:
    """is_locked=True takes priority over email_verified=True -> suspended."""
    customer = Customer(is_locked=True, email_verified=True)
    result = derive_subject_status_from_customer(customer)
    assert result == SubjectCustomerStatus.suspended


def test_derive_customer_verified_when_email_verified() -> None:
    """is_locked=False + email_verified=True -> SubjectCustomerStatus.verified."""
    customer = Customer(is_locked=False, email_verified=True)
    result = derive_subject_status_from_customer(customer)
    assert result == SubjectCustomerStatus.verified


def test_derive_customer_registered_default() -> None:
    """is_locked=False + email_verified=False -> SubjectCustomerStatus.registered."""
    customer = Customer(is_locked=False, email_verified=False)
    result = derive_subject_status_from_customer(customer)
    assert result == SubjectCustomerStatus.registered


# ---------------------------------------------------------------------------
# Employee derivation
# ---------------------------------------------------------------------------


def test_derive_employee_on_leave_when_locked() -> None:
    """is_locked=True on Employee -> SubjectEmployeeStatus.on_leave."""
    employee = Employee(is_locked=True)
    result = derive_subject_status_from_employee(employee)
    assert result == SubjectEmployeeStatus.on_leave


def test_derive_employee_active_default() -> None:
    """is_locked=False on Employee -> SubjectEmployeeStatus.active."""
    employee = Employee(is_locked=False)
    result = derive_subject_status_from_employee(employee)
    assert result == SubjectEmployeeStatus.active


# ---------------------------------------------------------------------------
# NHI derivation
# ---------------------------------------------------------------------------


def test_derive_nhi_locked_when_locked() -> None:
    """is_locked=True on NHI -> SubjectNHIStatus.locked."""
    nhi = NHI(is_locked=True)
    result = derive_subject_status_from_nhi(nhi)
    assert result == SubjectNHIStatus.locked


def test_derive_nhi_active_default() -> None:
    """is_locked=False on NHI -> SubjectNHIStatus.active."""
    nhi = NHI(is_locked=False)
    result = derive_subject_status_from_nhi(nhi)
    assert result == SubjectNHIStatus.active


# ---------------------------------------------------------------------------
# Dispatcher: kind/type mismatch raises ValueError
# ---------------------------------------------------------------------------


def test_derive_unknown_principal_type_raises() -> None:
    """derive_subject_status(SubjectKind.customer, Employee(...)) raises ValueError."""
    employee = Employee(is_locked=False)
    with pytest.raises(ValueError, match='expected Customer'):
        derive_subject_status(SubjectKind.customer, employee)


def test_derive_dispatcher_returns_string_value() -> None:
    """derive_subject_status returns a plain str, not a StrEnum instance."""
    customer = Customer(is_locked=False, email_verified=True)
    result = derive_subject_status(SubjectKind.customer, customer)
    assert result == 'verified'
    assert type(result) is str
