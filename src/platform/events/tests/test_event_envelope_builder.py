# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for new_event_envelope builder."""

from __future__ import annotations

import pytest
from src.core.context import correlation_id_var
from src.platform.events.schemas import new_event_envelope


def test_explicit_correlation_id_wins() -> None:
    token = correlation_id_var.set('ctx-value')
    try:
        envelope = new_event_envelope(
            event_type='test.entity.created',
            correlation_id='explicit',
        )
    finally:
        correlation_id_var.reset(token)

    assert envelope.correlation_id == 'explicit'


def test_fallback_to_contextvar() -> None:
    token = correlation_id_var.set('ctx-fallback-id')
    try:
        envelope = new_event_envelope(event_type='test.entity.created')
    finally:
        correlation_id_var.reset(token)

    assert envelope.correlation_id == 'ctx-fallback-id'


def test_no_contextvar_no_explicit_raises() -> None:
    # Ensure ContextVar is None (default state)
    assert correlation_id_var.get() is None

    with pytest.raises(ValueError, match='correlation_id is required'):
        new_event_envelope(event_type='test.entity.created')
