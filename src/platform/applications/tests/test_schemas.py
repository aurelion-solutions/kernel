# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ApplicationCreate / ApplicationUpdate schema validation."""

from pydantic import ValidationError
import pytest
from src.platform.applications.schemas import ApplicationCreate, ApplicationUpdate


@pytest.mark.parametrize(
    'code',
    [
        'Active Directory',  # spaces + uppercase
        'AD',  # uppercase
        ' ad',  # leading space
        '-ad',  # leading dash
        '_ad',  # leading underscore
        'ad!',  # invalid char
        'a' * 65,  # too long (65 chars)
        '',  # empty
    ],
)
def test_application_create_rejects_invalid_code(code: str) -> None:
    with pytest.raises(ValidationError):
        ApplicationCreate(name='Test', code=code)


@pytest.mark.parametrize(
    'code',
    [
        'ad',
        'jira',
        'stripe-billing',
        'stripe_billing',
        'customer-portal',
        'a',
        '0',
        '0ad',
        'a' * 64,
    ],
)
def test_application_create_accepts_valid_codes(code: str) -> None:
    schema = ApplicationCreate(name='Test', code=code)
    assert schema.code == code


def test_application_update_allows_none_code() -> None:
    schema = ApplicationUpdate(name='NewName')
    assert schema.code is None


def test_application_update_requires_at_least_one_field_rejects_all_none() -> None:
    with pytest.raises(ValidationError):
        ApplicationUpdate()
