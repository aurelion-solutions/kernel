# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for NHI schemas."""

import uuid

import pytest
from src.inventory.nhi.models import NHI, NHIAttribute
from src.inventory.nhi.schemas import (
    NHIAttributeCreate,
    NHIAttributeRead,
    NHICreate,
    NHIRead,
)


def test_nhi_create_accepts_valid_input() -> None:
    schema = NHICreate(
        external_id='nhi-1',
        name='Bot',
        kind='bot',
        is_locked=False,
        description=None,
    )
    assert schema.external_id == 'nhi-1'
    assert schema.is_locked is False


def test_nhi_read_from_orm_instance() -> None:
    nhi = NHI(
        id=uuid.uuid4(),
        external_id='ext',
        name='X',
        kind='service_account',
        description=None,
        is_locked=True,
        owner_employee_id=None,
        application_id=None,
    )
    schema = NHIRead.model_validate(nhi)
    assert schema.id == nhi.id
    assert schema.kind == 'service_account'


def test_nhi_attribute_create_accepts_valid_input() -> None:
    schema = NHIAttributeCreate(key='k', value='v')
    assert schema.key == 'k'
    assert schema.value == 'v'


def test_nhi_attribute_create_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        NHIAttributeCreate(key='', value='x')


def test_nhi_attribute_read_from_orm_instance() -> None:
    attr = NHIAttribute(
        id=uuid.uuid4(),
        nhi_id=uuid.uuid4(),
        key='title',
        value='Engineer',
    )
    schema = NHIAttributeRead.model_validate(attr)
    assert schema.key == 'title'
