# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeRecord schemas."""

import uuid

import pytest
from src.inventory.employee_records.models import (
    EmployeeRecord,
    EmployeeRecordAttribute,
)
from src.inventory.employee_records.schemas import (
    EmployeeRecordAttributeCreate,
    EmployeeRecordAttributeRead,
    EmployeeRecordCreate,
    EmployeeRecordRead,
)


def test_employee_record_create_accepts_valid_input() -> None:
    """EmployeeRecordCreate accepts valid input."""
    schema = EmployeeRecordCreate(
        external_id='rec-1',
        application_id=uuid.uuid4(),
        description=None,
    )
    assert schema.external_id == 'rec-1'
    assert schema.description is None


def test_employee_record_create_accepts_description() -> None:
    """EmployeeRecordCreate accepts optional description."""
    schema = EmployeeRecordCreate(
        external_id='rec-2',
        application_id=uuid.uuid4(),
        description='John from HRIS',
    )
    assert schema.description == 'John from HRIS'


def test_employee_record_create_rejects_empty_external_id() -> None:
    """EmployeeRecordCreate rejects empty external_id."""
    with pytest.raises(ValueError):
        EmployeeRecordCreate(external_id='', application_id=uuid.uuid4())


def test_employee_record_read_from_orm_instance() -> None:
    """EmployeeRecordRead builds from EmployeeRecord model."""
    rec = EmployeeRecord(
        id=uuid.uuid4(),
        external_id='rec-3',
        application_id=uuid.uuid4(),
        description='Lead',
    )
    schema = EmployeeRecordRead.model_validate(rec)
    assert schema.id == rec.id
    assert schema.external_id == 'rec-3'
    assert schema.description == 'Lead'


def test_employee_record_attribute_create_accepts_valid_input() -> None:
    """EmployeeRecordAttributeCreate accepts valid input."""
    schema = EmployeeRecordAttributeCreate(key='department', value='Engineering')
    assert schema.key == 'department'
    assert schema.value == 'Engineering'


def test_employee_record_attribute_create_rejects_empty_key() -> None:
    """EmployeeRecordAttributeCreate rejects empty key."""
    with pytest.raises(ValueError):
        EmployeeRecordAttributeCreate(key='', value='x')


def test_employee_record_attribute_create_rejects_empty_value() -> None:
    """EmployeeRecordAttributeCreate rejects empty value."""
    with pytest.raises(ValueError):
        EmployeeRecordAttributeCreate(key='k', value='')


def test_employee_record_attribute_read_from_orm_instance() -> None:
    """EmployeeRecordAttributeRead builds from EmployeeRecordAttribute model."""
    attr = EmployeeRecordAttribute(
        id=uuid.uuid4(),
        employee_record_id=uuid.uuid4(),
        key='title',
        value='Engineer',
    )
    schema = EmployeeRecordAttributeRead.model_validate(attr)
    assert schema.id == attr.id
    assert schema.employee_record_id == attr.employee_record_id
    assert schema.key == 'title'
    assert schema.value == 'Engineer'
