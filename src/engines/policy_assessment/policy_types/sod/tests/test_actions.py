# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for policy_assessment.sod engine actions.

Covers:
- Registry presence for both actions.
- Dispatch with invalid args raises ActionArgsValidationError.
- Dispatch with valid args returns a result that round-trips result_schema.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
from typing import cast
from unittest.mock import MagicMock
import uuid

import pytest
from src.platform.logs.service import LogService, noop_log_service
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    RegisteredAction,
)

_ENGINE = 'policy_assessment.sod'


def _make_ctx() -> ActionContext:
    return ActionContext(
        session=MagicMock(),
        log_service=cast(LogService, noop_log_service),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id=None,
    )


_SOD_ACTIONS_MODULE = 'src.engines.policy_assessment.policy_types.sod.actions'


@pytest.fixture(autouse=True)
def _registry_isolation() -> Iterator[None]:
    """Clear registry, re-import actions module to re-register, then clean up."""
    import sys  # noqa: PLC0415

    ACTION_REGISTRY._clear_for_tests()
    sys.modules.pop(_SOD_ACTIONS_MODULE, None)
    importlib.import_module(_SOD_ACTIONS_MODULE)
    yield
    ACTION_REGISTRY._clear_for_tests()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('action_name', ['evaluate', 'what_if'])
def test_action_is_registered(action_name: str) -> None:
    """Both sod actions are registered with correct metadata."""
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, action_name)
    assert record.engine == _ENGINE
    assert record.action == action_name
    assert record.idempotent is True
    assert record.args_schema is not None
    assert record.result_schema is not None


# ---------------------------------------------------------------------------
# Invalid args tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize('action_name', ['evaluate', 'what_if'])
async def test_dispatch_invalid_args_raises(action_name: str) -> None:
    """Dispatch with garbage raw_args raises ActionArgsValidationError."""
    ctx = _make_ctx()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            action_name,
            raw_args={'not_a_valid_field': 'garbage'},
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_evaluate_returns_result(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Dispatch evaluate with a valid subject_id returns a serialisable result."""
    async with session_factory() as session:
        ctx = ActionContext(
            session=session,
            log_service=cast(LogService, noop_log_service),
            pipeline_run_id=uuid.uuid4(),
            step_run_id=uuid.uuid4(),
            attempt=1,
            worker_id=None,
        )
        raw_result = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'evaluate',
            raw_args={'subject_id': str(uuid.uuid4())},
            ctx=ctx,
        )
        # Round-trip through result_schema
        record = ACTION_REGISTRY.get(_ENGINE, 'evaluate')
        validated = record.result_schema.model_validate(raw_result)
        assert hasattr(validated, 'violations')
        assert isinstance(validated.violations, list)


@pytest.mark.asyncio
async def test_dispatch_what_if_returns_result(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Dispatch what_if with empty overrides returns a serialisable result."""
    async with session_factory() as session:
        ctx = ActionContext(
            session=session,
            log_service=cast(LogService, noop_log_service),
            pipeline_run_id=uuid.uuid4(),
            step_run_id=uuid.uuid4(),
            attempt=1,
            worker_id=None,
        )
        raw_result = await ACTION_REGISTRY.dispatch(
            _ENGINE,
            'what_if',
            raw_args={'subject_id': str(uuid.uuid4()), 'capability_overrides': []},
            ctx=ctx,
        )
        record = ACTION_REGISTRY.get(_ENGINE, 'what_if')
        validated = record.result_schema.model_validate(raw_result)
        assert hasattr(validated, 'violations')
        assert isinstance(validated.violations, list)
