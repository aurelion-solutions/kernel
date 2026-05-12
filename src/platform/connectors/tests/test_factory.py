# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for src.platform.connectors.factory (Phase 18 Step 9e)."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
import src.platform.connectors.factory as factory_module

# ---------------------------------------------------------------------------
# Fixture: reset module state around every test
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_factory() -> Iterator[None]:
    """Ensure the module-level client is None before and after each test."""
    factory_module._process_connector_client = None
    yield
    factory_module._process_connector_client = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_before_set_raises_runtime_error() -> None:
    """get_process_connector_client raises RuntimeError when not initialised."""
    with pytest.raises(RuntimeError, match='not initialised'):
        factory_module.get_process_connector_client()


def test_set_then_get_returns_same_client() -> None:
    """get_process_connector_client returns the exact object passed to set."""
    client = MagicMock()
    factory_module.set_process_connector_client(client)
    assert factory_module.get_process_connector_client() is client


def test_set_overwrites_previous() -> None:
    """A second set_process_connector_client call replaces the previous client."""
    first = MagicMock()
    second = MagicMock()
    factory_module.set_process_connector_client(first)
    factory_module.set_process_connector_client(second)
    assert factory_module.get_process_connector_client() is second
