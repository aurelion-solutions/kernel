# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for src.core.http.errors.translate_service_errors."""

from fastapi import HTTPException
import pytest
from src.core.http.errors import ErrorMap, translate_service_errors


def test_translate_static_detail_maps_to_http_exception() -> None:
    """A mapped exception with a static detail raises the correct HTTPException."""
    with pytest.raises(HTTPException) as exc_info:
        with translate_service_errors({ValueError: (400, 'bad')}):
            raise ValueError('boom')

    http_exc = exc_info.value
    assert http_exc.status_code == 400
    assert http_exc.detail == 'bad'
    assert http_exc.__cause__ is None
    assert http_exc.__suppress_context__ is True


def test_translate_callable_detail_uses_exception_attribute() -> None:
    """A callable detail receives the exception and interpolates its attributes."""

    class KeyDup(Exception):
        def __init__(self, key: str) -> None:
            super().__init__(key)
            self.key = key

    with pytest.raises(HTTPException) as exc_info:
        with translate_service_errors({KeyDup: (409, lambda exc: f'dup:{exc.key}')}):  # type: ignore[attr-defined]
            raise KeyDup('email')

    http_exc = exc_info.value
    assert http_exc.status_code == 409
    assert http_exc.detail == 'dup:email'


def test_translate_unknown_exception_propagates() -> None:
    """An exception not in the mapping propagates unchanged."""
    original = RuntimeError('x')

    with pytest.raises(RuntimeError) as exc_info:
        with translate_service_errors({ValueError: (400, 'bad')}):
            raise original

    assert exc_info.value is original
    assert str(exc_info.value) == 'x'


def test_translate_no_exception_is_noop() -> None:
    """The happy path exits cleanly without raising."""
    result = None
    with translate_service_errors({ValueError: (400, 'bad')}):
        result = 42
    assert result == 42


def test_translate_mapping_respects_subclass_order() -> None:
    """Insertion order resolves parent/child ambiguity — child must come first."""

    class Parent(Exception):
        pass

    class Child(Parent):
        pass

    # Child before Parent → Child gets 409, Parent gets 422.
    mapping: ErrorMap = {Child: (409, 'child'), Parent: (422, 'parent')}

    with pytest.raises(HTTPException) as exc_info:
        with translate_service_errors(mapping):
            raise Child()
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == 'child'

    with pytest.raises(HTTPException) as exc_info:
        with translate_service_errors(mapping):
            raise Parent()
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == 'parent'
