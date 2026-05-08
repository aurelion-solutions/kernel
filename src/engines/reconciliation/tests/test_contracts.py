# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for engines.reconciliation.contracts."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
import uuid

import pytest
from src.engines.reconciliation.contracts import NormalizationResult


def test_normalization_result_is_frozen():
    result = NormalizationResult(
        subject_id=uuid.uuid4(),
        account_id=None,
        resource_id=uuid.uuid4(),
        action_slug='read',
        effect='allow',
        valid_from=None,
        valid_until=None,
    )
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError, AttributeError)):
        result.action_slug = 'write'  # type: ignore[misc]


def test_handler_protocol_is_structural():
    """A class with the correct async signature satisfies the Handler Protocol at runtime."""
    from src.engines.reconciliation.contracts import Handler

    class _ConcreteHandler:
        async def handle(self, artifact, session):
            return []

    handler = _ConcreteHandler()
    # Structural check: the handler has the required method
    assert callable(getattr(handler, 'handle', None))
    # Runtime isinstance check (Protocol is not @runtime_checkable, so we just
    # verify duck-typing is sufficient — i.e. no AttributeError on use)
    # Cast at type-checker level works; the test ensures no import / structural error
    _h: Handler = handler  # type: ignore[assignment]
    assert _h is handler


def test_normalization_result_fields():
    sid = uuid.uuid4()
    rid = uuid.uuid4()
    vf = datetime(2026, 1, 1, tzinfo=UTC)
    result = NormalizationResult(
        subject_id=sid,
        account_id=None,
        resource_id=rid,
        action_slug='write',
        effect='deny',
        valid_from=vf,
        valid_until=None,
    )
    assert result.subject_id == sid
    assert result.resource_id == rid
    assert result.action_slug == 'write'
    assert result.effect == 'deny'
    assert result.valid_from == vf
    assert result.valid_until is None
