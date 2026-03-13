# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Secret schemas."""

from pydantic import ValidationError
import pytest
from src.inventory.secrets.schemas import SecretCreate, SecretDelete, SecretRead


def test_secret_create_accepts_valid_payload() -> None:
    """SecretCreate accepts valid payload with required fields."""
    schema = SecretCreate(key='app/token', provider='file', namespace='default', value='secret123')
    assert schema.key == 'app/token'
    assert schema.provider == 'file'
    assert schema.namespace == 'default'
    assert schema.value == 'secret123'


def test_secret_create_rejects_invalid_key_format() -> None:
    """SecretCreate rejects key that is not path-like."""
    with pytest.raises(ValidationError) as exc_info:
        SecretCreate(key='invalid key', provider='file', namespace='default', value='x')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('key',) for e in errors)


def test_secret_create_accepts_path_like_key() -> None:
    """SecretCreate accepts path-like key (segment/segment)."""
    schema = SecretCreate(key='team-a/prod/api-key', provider='vault', namespace='prod', value='x')
    assert schema.key == 'team-a/prod/api-key'


def test_secret_create_rejects_empty_namespace() -> None:
    """SecretCreate rejects empty namespace."""
    with pytest.raises(ValidationError) as exc_info:
        SecretCreate(key='a/b', provider='file', namespace='', value='x')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('namespace',) for e in errors)


def test_secret_create_rejects_invalid_namespace() -> None:
    """SecretCreate rejects namespace with invalid characters."""
    with pytest.raises(ValidationError) as exc_info:
        SecretCreate(key='a/b', provider='file', namespace='bad space', value='x')
    errors = exc_info.value.errors()
    assert any(e['loc'] == ('namespace',) for e in errors)


def test_secret_read_has_no_value_field() -> None:
    """SecretRead does not include value field."""
    assert 'value' not in SecretRead.model_fields
    schema = SecretRead(key='app/token', provider='file', namespace='default')
    assert schema.key == 'app/token'


def test_secret_delete_has_no_value_field() -> None:
    """SecretDelete does not include value field."""
    assert 'value' not in SecretDelete.model_fields
    schema = SecretDelete(key='app/token', provider='file', namespace='default')
    assert schema.key == 'app/token'
