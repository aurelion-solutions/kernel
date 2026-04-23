# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Schema tests for ActionRead."""

from __future__ import annotations

from datetime import UTC, datetime
import types

from pydantic import ValidationError
import pytest
from src.inventory.actions.schemas import ActionRead

_TS = datetime(2026, 4, 24, 0, 0, 0, tzinfo=UTC)


def test_action_read_from_attributes() -> None:
    obj = types.SimpleNamespace(id=1, slug='read', description='Observe a resource.', created_at=_TS)
    schema = ActionRead.model_validate(obj)
    assert schema.id == 1
    assert schema.slug == 'read'
    assert schema.description == 'Observe a resource.'
    assert schema.created_at == _TS


def test_action_read_allows_null_description() -> None:
    obj = types.SimpleNamespace(id=2, slug='write', description=None, created_at=_TS)
    schema = ActionRead.model_validate(obj)
    assert schema.description is None


def test_action_read_rejects_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        ActionRead.model_validate({'id': 1, 'slug': 'read'})
