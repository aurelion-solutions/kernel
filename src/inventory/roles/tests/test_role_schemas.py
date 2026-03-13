# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for RoleDTO validation."""

from pydantic import ValidationError
import pytest
from src.inventory.roles.schemas import RoleDTO


def test_role_dto_accepts_valid_payload():
    """RoleDTO accepts valid payload with required identifier."""
    dto = RoleDTO(identifier='role_123', name='admin')
    assert dto.identifier == 'role_123'
    assert dto.name == 'admin'
    assert dto.is_active is True


def test_role_dto_accepts_full_payload():
    """RoleDTO accepts full payload with all optional fields."""
    dto = RoleDTO(
        identifier='role_456',
        name='viewer',
        display_name='Viewer Role',
        type='builtin',
        is_active=False,
        meta={'source': 'connector'},
    )
    assert dto.identifier == 'role_456'
    assert dto.name == 'viewer'
    assert dto.display_name == 'Viewer Role'
    assert dto.type == 'builtin'
    assert dto.is_active is False
    assert dto.meta == {'source': 'connector'}


def test_role_dto_rejects_missing_identifier():
    """RoleDTO rejects payload without identifier."""
    with pytest.raises(ValidationError) as exc_info:
        RoleDTO(name='admin')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('identifier',) for e in errors)


def test_role_dto_rejects_empty_identifier():
    """RoleDTO rejects empty identifier."""
    with pytest.raises(ValidationError) as exc_info:
        RoleDTO(identifier='')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('identifier',) for e in errors)
