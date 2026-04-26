# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for src.core.context."""

from src.core.context import correlation_id_var, current_correlation_id


def test_default_is_none() -> None:
    # ContextVar default is None; no request context is active.
    assert current_correlation_id() is None


def test_set_and_read_within_token() -> None:
    token = correlation_id_var.set('test-cid-42')
    try:
        assert current_correlation_id() == 'test-cid-42'
    finally:
        correlation_id_var.reset(token)

    assert current_correlation_id() is None
