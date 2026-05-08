# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for lazy engine / session factory in src.core.db.session."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _register_providers() -> None:
    from src.platform.secrets.factory import register_default_providers

    register_default_providers()


def test_get_engine_is_cached() -> None:
    """get_engine() returns the same instance on repeated calls."""
    from src.core.db.session import get_engine

    get_engine.cache_clear()
    e1 = get_engine()
    e2 = get_engine()
    assert e1 is e2
    get_engine.cache_clear()


def test_get_session_factory_is_cached() -> None:
    """get_session_factory() returns the same instance on repeated calls."""
    from src.core.db.session import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    f1 = get_session_factory()
    f2 = get_session_factory()
    assert f1 is f2
    get_session_factory.cache_clear()
    get_engine.cache_clear()


def test_cache_clear_rebuilds_engine() -> None:
    """After cache_clear(), get_engine() returns a fresh instance."""
    from src.core.db.session import get_engine, get_session_factory

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    e1 = get_engine()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    e2 = get_engine()
    assert e1 is not e2
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def test_no_module_level_singleton() -> None:
    """session.py must not export 'engine' or 'SessionLocal' module-level names."""
    import importlib

    session_mod = importlib.import_module('src.core.db.session')
    assert not hasattr(session_mod, 'engine'), "Module-level 'engine' singleton must not exist"
    assert not hasattr(session_mod, 'SessionLocal'), "Module-level 'SessionLocal' singleton must not exist"
