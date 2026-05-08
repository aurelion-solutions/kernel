# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Person schemas."""

import uuid

import pytest
from src.inventory.persons.models import Person, PersonAttribute
from src.inventory.persons.schemas import (
    PersonAttributeCreate,
    PersonAttributeRead,
    PersonCreate,
    PersonRead,
)


def test_person_create_accepts_valid_input() -> None:
    """PersonCreate accepts valid input."""
    schema = PersonCreate(external_id='ext-1', full_name='Alice')
    assert schema.external_id == 'ext-1'
    assert schema.full_name == 'Alice'


def test_person_create_rejects_empty_external_id() -> None:
    """PersonCreate rejects empty external_id."""
    with pytest.raises(ValueError):
        PersonCreate(external_id='', full_name='x')


def test_person_create_rejects_empty_full_name() -> None:
    """PersonCreate rejects empty full_name."""
    with pytest.raises(ValueError):
        PersonCreate(external_id='x', full_name='')


def test_person_read_from_orm_instance() -> None:
    """PersonRead builds from Person model."""
    person = Person(
        id=uuid.uuid4(),
        external_id='ext-2',
        full_name='Bob Jones',
    )
    schema = PersonRead.model_validate(person)
    assert schema.id == person.id
    assert schema.external_id == 'ext-2'
    assert schema.full_name == 'Bob Jones'


def test_person_attribute_create_accepts_valid_input() -> None:
    """PersonAttributeCreate accepts valid input."""
    schema = PersonAttributeCreate(key='department', value='Engineering')
    assert schema.key == 'department'
    assert schema.value == 'Engineering'


def test_person_attribute_create_rejects_empty_key() -> None:
    """PersonAttributeCreate rejects empty key."""
    with pytest.raises(ValueError):
        PersonAttributeCreate(key='', value='x')


def test_person_attribute_create_rejects_empty_value() -> None:
    """PersonAttributeCreate rejects empty value."""
    with pytest.raises(ValueError):
        PersonAttributeCreate(key='k', value='')


def test_person_attribute_read_from_orm_instance() -> None:
    """PersonAttributeRead builds from PersonAttribute model."""
    attr = PersonAttribute(
        id=uuid.uuid4(),
        person_id=uuid.uuid4(),
        key='title',
        value='Engineer',
    )
    schema = PersonAttributeRead.model_validate(attr)
    assert schema.id == attr.id
    assert schema.person_id == attr.person_id
    assert schema.key == 'title'
    assert schema.value == 'Engineer'
