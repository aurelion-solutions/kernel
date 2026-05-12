# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for access_analysis.reports engine actions.

Covers:
- Registry presence for the deterministic action.
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

_ENGINE = 'access_analysis.reports'
_ACTION = 'deterministic'


def _make_ctx() -> ActionContext:
    return ActionContext(
        session=MagicMock(),
        log_service=cast(LogService, noop_log_service),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id=None,
    )


_ACTIONS_MODULE = 'src.engines.access_analysis.reports.actions'


@pytest.fixture(autouse=True)
def _registry_isolation() -> Iterator[None]:
    """Clear registry, re-import actions module to re-register, then clean up."""
    import sys  # noqa: PLC0415

    ACTION_REGISTRY._clear_for_tests()
    sys.modules.pop(_ACTIONS_MODULE, None)
    importlib.import_module(_ACTIONS_MODULE)
    yield
    ACTION_REGISTRY._clear_for_tests()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_action_is_registered() -> None:
    """reports.deterministic action is registered with correct metadata."""
    record: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, _ACTION)
    assert record.engine == _ENGINE
    assert record.action == _ACTION
    assert record.idempotent is True
    assert record.args_schema is not None
    assert record.result_schema is not None


# ---------------------------------------------------------------------------
# Invalid args tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_invalid_args_raises() -> None:
    """Dispatch with args that violate field constraints raises ActionArgsValidationError."""
    ctx = _make_ctx()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _ACTION,
            # top_findings_limit must be ge=1 le=100; 0 and 999 are both invalid
            raw_args={'top_findings_limit': 0},
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_deterministic_returns_result(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Dispatch deterministic with default args against an empty DB returns a valid report."""
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
            _ACTION,
            raw_args={},
            ctx=ctx,
        )
        record = ACTION_REGISTRY.get(_ENGINE, _ACTION)
        validated = record.result_schema.model_validate(raw_result)
        assert hasattr(validated, 'summary')
        assert hasattr(validated, 'top_findings')
        assert hasattr(validated, 'recommendations')
        assert hasattr(validated, 'executive_summary')
        assert hasattr(validated, 'generated_at')
