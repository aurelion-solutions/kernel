# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for capabilities.reconciliation.registry."""

from __future__ import annotations

import pytest
from src.capabilities.reconciliation.contracts import HandlerAlreadyRegisteredError
from src.capabilities.reconciliation.registry import (
    _reset_registry_for_tests,
    get_handler,
    list_registered_types,
    register_handler,
)


@pytest.fixture(autouse=True)
def reset_registry():
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


class _DummyHandler:
    async def handle(self, artifact, session):
        return []


def test_register_and_get_handler():
    handler = _DummyHandler()
    register_handler('my_type', handler)
    assert get_handler('my_type') is handler


def test_get_unknown_type_returns_none():
    assert get_handler('nonexistent') is None


def test_register_duplicate_raises():
    register_handler('dup_type', _DummyHandler())
    with pytest.raises(HandlerAlreadyRegisteredError):
        register_handler('dup_type', _DummyHandler())


@pytest.mark.parametrize(
    'bad_type',
    ['', 'UPPERCASE', 'camelCase', 'has.dot', '123start', ' space'],
)
def test_register_invalid_artifact_type_raises(bad_type: str):
    with pytest.raises(ValueError):
        register_handler(bad_type, _DummyHandler())


def test_list_registered_types_sorted():
    register_handler('zebra', _DummyHandler())
    register_handler('apple', _DummyHandler())
    assert list_registered_types() == ['apple', 'zebra']


def test_reset_registry_for_tests_clears_state():
    register_handler('temp_type', _DummyHandler())
    _reset_registry_for_tests()
    assert get_handler('temp_type') is None
    assert list_registered_types() == []
