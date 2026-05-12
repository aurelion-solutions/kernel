# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ActionRegistry, @register_action decorator, and ActionContext."""

from __future__ import annotations

from collections.abc import Iterator
import importlib
from typing import Any, cast
from unittest.mock import MagicMock
import uuid

from pydantic import BaseModel
import pytest
from src.platform.logs.service import LogService
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    ActionHandler,
    ActionNotFoundError,
    ActionResultValidationError,
    DuplicateActionError,
    register_action,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _Args(BaseModel):
    value: int


class _Result(BaseModel):
    doubled: int


def _make_context() -> ActionContext:
    """Return a minimal ActionContext backed by mocks."""
    from src.platform.logs.service import noop_log_service

    session = MagicMock()
    return ActionContext(
        session=session,
        log_service=cast(LogService, noop_log_service),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id=None,
    )


@pytest.fixture(autouse=True)
def _clear_registry() -> Iterator[None]:
    """Reset the singleton before (and after) every test."""
    ACTION_REGISTRY._clear_for_tests()
    yield
    ACTION_REGISTRY._clear_for_tests()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_register_action_success() -> None:
    """Decorator registers the action; get() returns the record; idempotent defaults True."""

    async def _handler(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        assert isinstance(args, _Args)
        return {'doubled': args.value * 2}

    register_action('eng', 'act', _Args, _Result)(_handler)

    record = ACTION_REGISTRY.get('eng', 'act')
    assert record.engine == 'eng'
    assert record.action == 'act'
    assert record.handler is _handler
    assert record.args_schema is _Args
    assert record.result_schema is _Result
    assert record.idempotent is True


def test_duplicate_registration_raises() -> None:
    """Registering the same (engine, action) twice raises DuplicateActionError."""

    async def _h1(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {}

    async def _h2(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {}

    ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, _h1)

    with pytest.raises(DuplicateActionError):
        ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, _h2)


def test_action_not_found_raises() -> None:
    """get() and dispatch() on unknown key raise ActionNotFoundError."""

    with pytest.raises(ActionNotFoundError):
        ACTION_REGISTRY.get('missing', 'action')


@pytest.mark.asyncio
async def test_action_not_found_dispatch_raises() -> None:
    ctx = _make_context()
    with pytest.raises(ActionNotFoundError):
        await ACTION_REGISTRY.dispatch('missing', 'action', {}, ctx)


@pytest.mark.asyncio
async def test_dispatch_validates_args() -> None:
    """dispatch() parses raw_args into args_schema; handler receives model instance."""
    received: list[Any] = []

    async def _handler(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        assert isinstance(args, _Args)
        received.append(args)
        return {'doubled': args.value * 2}

    ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, _handler)
    ctx = _make_context()
    await ACTION_REGISTRY.dispatch('eng', 'act', {'value': 7}, ctx)

    assert len(received) == 1
    assert isinstance(received[0], _Args)
    assert received[0].value == 7


@pytest.mark.asyncio
async def test_dispatch_rejects_invalid_args() -> None:
    """Bad args raise ActionArgsValidationError."""

    async def _handler(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {'doubled': 0}

    ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, _handler)
    ctx = _make_context()

    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch('eng', 'act', {'value': 'not-an-int'}, ctx)


@pytest.mark.asyncio
async def test_dispatch_validates_result() -> None:
    """Handler returning a dict incompatible with result_schema raises ActionResultValidationError."""

    async def _bad_handler(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {'unexpected_key': 42}  # missing 'doubled'

    ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, _bad_handler)
    ctx = _make_context()

    with pytest.raises(ActionResultValidationError):
        await ACTION_REGISTRY.dispatch('eng', 'act', {'value': 1}, ctx)


@pytest.mark.asyncio
async def test_dispatch_returns_model_dump() -> None:
    """dispatch() returns a plain dict, not a model instance (contract for runner JSONB)."""

    async def _handler(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        assert isinstance(args, _Args)
        return {'doubled': args.value * 2}

    ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, _handler)
    ctx = _make_context()
    result = await ACTION_REGISTRY.dispatch('eng', 'act', {'value': 5}, ctx)

    assert isinstance(result, dict)
    assert result == {'doubled': 10}


def test_idempotent_flag_recorded() -> None:
    """idempotent=False is stored faithfully on the record."""

    async def _h(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {}

    ACTION_REGISTRY.register('eng', 'act', _Args, _Result, False, _h)
    record = ACTION_REGISTRY.get('eng', 'act')
    assert record.idempotent is False


def test_non_basemodel_schema_rejected() -> None:
    """Non-BaseModel schemas raise TypeError at registration."""

    async def _h(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {}

    with pytest.raises(TypeError):
        ACTION_REGISTRY.register('eng', 'act', dict, _Result, True, _h)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        ACTION_REGISTRY.register('eng', 'act', _Args, dict, True, _h)  # type: ignore[arg-type]


def test_sync_handler_rejected() -> None:
    """A plain def (non-async) handler raises TypeError at registration."""

    def _sync_handler(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {}

    with pytest.raises(TypeError):
        ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, cast(ActionHandler, _sync_handler))


def test_schema_export_via_model_json_schema() -> None:
    """Smoke test for Step 11 .well-known: model_json_schema() returns a dict."""

    async def _h(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {'doubled': 0}

    ACTION_REGISTRY.register('eng', 'act', _Args, _Result, True, _h)
    record = ACTION_REGISTRY.get('eng', 'act')

    assert isinstance(record.args_schema.model_json_schema(), dict)
    assert isinstance(record.result_schema.model_json_schema(), dict)


def test_no_side_effects_on_import() -> None:
    """Reloading registry module must not register anything; singleton starts empty."""
    import src.platform.orchestrator.registry as reg_module

    importlib.reload(reg_module)
    # After reload the module-level ACTION_REGISTRY is a fresh instance
    assert reg_module.ACTION_REGISTRY.all() == []


def test_register_empty_engine_raises() -> None:
    """Registering with an empty engine string raises ValueError."""

    async def _h(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError, match='engine'):
        ACTION_REGISTRY.register('', 'act', _Args, _Result, True, _h)


def test_register_empty_action_raises() -> None:
    """Registering with an empty action string raises ValueError."""

    async def _h(args: BaseModel, ctx: ActionContext) -> dict[str, Any]:
        return {}

    with pytest.raises(ValueError, match='action'):
        ACTION_REGISTRY.register('eng', '', _Args, _Result, True, _h)
