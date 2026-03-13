# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Employee schemas."""

import uuid

import pytest
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.employees.schemas import (
    EmployeeAttributeCreate,
    EmployeeAttributeRead,
    EmployeeCreate,
    EmployeeRead,
)


def test_employee_create_accepts_valid_input() -> None:
    """EmployeeCreate accepts valid input."""
    schema = EmployeeCreate(
        person_id=uuid.uuid4(),
        is_locked=False,
        description=None,
    )
    assert schema.is_locked is False
    assert schema.description is None


def test_employee_create_accepts_description() -> None:
    """EmployeeCreate accepts optional description."""
    schema = EmployeeCreate(
        person_id=uuid.uuid4(),
        description='Engineer',
    )
    assert schema.description == 'Engineer'


def test_employee_read_from_orm_instance() -> None:
    """EmployeeRead builds from Employee model."""
    emp = Employee(
        id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        is_locked=True,
        description='Lead',
    )
    schema = EmployeeRead.model_validate(emp)
    assert schema.id == emp.id
    assert schema.is_locked is True
    assert schema.description == 'Lead'


def test_employee_attribute_create_accepts_valid_input() -> None:
    """EmployeeAttributeCreate accepts valid input."""
    schema = EmployeeAttributeCreate(key='department', value='Engineering')
    assert schema.key == 'department'
    assert schema.value == 'Engineering'


def test_employee_attribute_create_rejects_empty_key() -> None:
    """EmployeeAttributeCreate rejects empty key."""
    with pytest.raises(ValueError):
        EmployeeAttributeCreate(key='', value='x')


def test_employee_attribute_create_rejects_empty_value() -> None:
    """EmployeeAttributeCreate rejects empty value."""
    with pytest.raises(ValueError):
        EmployeeAttributeCreate(key='k', value='')


def test_employee_attribute_read_from_orm_instance() -> None:
    """EmployeeAttributeRead builds from EmployeeAttribute model."""
    attr = EmployeeAttribute(
        id=uuid.uuid4(),
        employee_id=uuid.uuid4(),
        key='title',
        value='Engineer',
    )
    schema = EmployeeAttributeRead.model_validate(attr)
    assert schema.id == attr.id
    assert schema.employee_id == attr.employee_id
    assert schema.key == 'title'
    assert schema.value == 'Engineer'
