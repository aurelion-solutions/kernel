# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for PrivilegeDTO validation."""

from pydantic import ValidationError
import pytest
from src.inventory.privileges.schemas import PrivilegeDTO


def test_privilege_dto_accepts_valid_payload():
    """PrivilegeDTO accepts valid payload with required identifier."""
    dto = PrivilegeDTO(identifier='priv_123', name='read')
    assert dto.identifier == 'priv_123'
    assert dto.name == 'read'
    assert dto.is_active is True


def test_privilege_dto_accepts_full_payload():
    """PrivilegeDTO accepts full payload with all optional fields."""
    dto = PrivilegeDTO(
        identifier='priv_456',
        name='write',
        display_name='Write Access',
        type='permission',
        is_active=False,
        meta={'source': 'connector'},
    )
    assert dto.identifier == 'priv_456'
    assert dto.name == 'write'
    assert dto.display_name == 'Write Access'
    assert dto.type == 'permission'
    assert dto.is_active is False
    assert dto.meta == {'source': 'connector'}


def test_privilege_dto_rejects_missing_identifier():
    """PrivilegeDTO rejects payload without identifier."""
    with pytest.raises(ValidationError) as exc_info:
        PrivilegeDTO(name='read')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('identifier',) for e in errors)


def test_privilege_dto_rejects_empty_identifier():
    """PrivilegeDTO rejects empty identifier."""
    with pytest.raises(ValidationError) as exc_info:
        PrivilegeDTO(identifier='')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('identifier',) for e in errors)
