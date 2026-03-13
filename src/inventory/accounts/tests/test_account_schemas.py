# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccountDTO validation."""

from pydantic import ValidationError
import pytest
from src.inventory.accounts.schemas import AccountDTO, AccountStatus


def test_account_dto_accepts_valid_payload():
    """AccountDTO accepts valid payload with required identifier."""
    dto = AccountDTO(identifier='user_123', username='alice')
    assert dto.identifier == 'user_123'
    assert dto.username == 'alice'
    assert dto.is_active is True


def test_account_dto_accepts_full_payload():
    """AccountDTO accepts full payload with all optional fields."""
    dto = AccountDTO(
        identifier='user_456',
        username='bob',
        display_name='Bob Smith',
        email='bob@example.com',
        is_active=False,
        is_privileged=True,
        mfa_enabled=True,
        meta={'source': 'connector'},
    )
    assert dto.identifier == 'user_456'
    assert dto.username == 'bob'
    assert dto.display_name == 'Bob Smith'
    assert dto.email == 'bob@example.com'
    assert dto.is_active is False
    assert dto.is_privileged is True
    assert dto.mfa_enabled is True
    assert dto.meta == {'source': 'connector'}


def test_account_dto_rejects_missing_identifier():
    """AccountDTO rejects payload without identifier."""
    with pytest.raises(ValidationError) as exc_info:
        AccountDTO(username='alice')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('identifier',) for e in errors)


def test_account_dto_rejects_empty_identifier():
    """AccountDTO rejects empty identifier."""
    with pytest.raises(ValidationError) as exc_info:
        AccountDTO(identifier='')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('identifier',) for e in errors)


def test_account_dto_accepts_status_value():
    """AccountDTO accepts a valid status string and returns the enum instance."""
    dto = AccountDTO(identifier='x', status='suspended')  # type: ignore[arg-type]
    assert dto.status == AccountStatus.suspended


def test_account_dto_defaults_status_to_none():
    """AccountDTO.status is None when not supplied."""
    dto = AccountDTO(identifier='x')
    assert dto.status is None


def test_account_dto_rejects_invalid_status():
    """AccountDTO rejects an invalid status value."""
    with pytest.raises(ValidationError):
        AccountDTO(identifier='x', status='bogus')  # type: ignore[arg-type]
