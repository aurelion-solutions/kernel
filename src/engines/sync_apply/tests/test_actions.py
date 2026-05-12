# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for sync_apply engine actions (Phase 18 Step 9d).

Covers:
- Registration of sync_apply.apply with correct metadata.
- Dispatch with invalid args raises ActionArgsValidationError.
- Happy-path dispatch for dry_run and auto_apply with mocked lake/catalog/events.
- Commit ownership: session.in_transaction() is True after apply action dispatch.
"""

from __future__ import annotations

from collections.abc import Iterator
import importlib
import sys
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
import uuid
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.engines.sync_apply.actions import SyncApplyApplyArgs, SyncApplyApplyResult
from src.engines.sync_apply.lake_writer import PreflightRecoveryResult
from src.platform.logs.service import LogService, noop_log_service
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionArgsValidationError,
    ActionContext,
    RegisteredAction,
)

_ENGINE = 'sync_apply'
_ACTION = 'apply'
_ACTIONS_MODULE = 'src.engines.sync_apply.actions'

_NOW_ISO = '2026-05-11T00:00:00+00:00'
_SUBJECT_ID = uuid.UUID('bbbbbbbb-0000-0000-0000-000000000002')
_RESOURCE_ID = uuid.UUID('cccccccc-0000-0000-0000-000000000003')
_ACTION_ID = 42


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx_mock() -> ActionContext:
    """Build an ActionContext with a MagicMock session (no DB needed)."""
    return ActionContext(
        session=MagicMock(),
        log_service=cast(LogService, noop_log_service),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id=None,
    )


def _make_ctx(session: AsyncSession) -> ActionContext:
    """Build an ActionContext with a real async session."""
    return ActionContext(
        session=session,
        log_service=cast(LogService, noop_log_service),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id=None,
    )


# ---------------------------------------------------------------------------
# Registry isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _registry_isolation() -> Iterator[None]:
    """Clear registry, re-import actions module to re-register, then clean up."""
    ACTION_REGISTRY._clear_for_tests()
    sys.modules.pop(_ACTIONS_MODULE, None)
    importlib.import_module(_ACTIONS_MODULE)
    yield
    ACTION_REGISTRY._clear_for_tests()


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_reconciliation_run(session: AsyncSession) -> ReconciliationRun:
    """Create and persist a minimal ReconciliationRun in pending_apply status."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from src.platform.applications.models import Application  # noqa: PLC0415

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    run = ReconciliationRun(
        application_id=app.id,
        status=ReconciliationRunStatus.pending_apply,
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def _seed_delta_item(
    session: AsyncSession,
    run: ReconciliationRun,
    *,
    operation: ReconciliationDeltaOperation = ReconciliationDeltaOperation.create,
    status: ReconciliationDeltaItemStatus = ReconciliationDeltaItemStatus.approved,
) -> ReconciliationDeltaItem:
    """Seed a single delta item for a run."""
    item = ReconciliationDeltaItem(
        reconciliation_run_id=run.id,
        operation=operation,
        natural_key_hash='0' * 64,
        subject_id=_SUBJECT_ID,
        account_id=None,
        resource_id=_RESOURCE_ID,
        action_id=_ACTION_ID,
        effect='allow',
        status=status,
        before_json=None,
        after_json={
            'effect': 'allow',
            'valid_from': _NOW_ISO,
            'observed_at': _NOW_ISO,
            'created_at': _NOW_ISO,
            'valid_until': None,
            'revoked_at': None,
            'latest_batch_id': None,
        },
    )
    session.add(item)
    await session.flush()
    return item


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_apply_action_registered() -> None:
    """sync_apply.apply is registered with idempotent=True and correct schemas."""
    rec: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, _ACTION)
    assert rec.idempotent is True
    assert rec.args_schema is not None
    assert rec.args_schema.__name__ == SyncApplyApplyArgs.__name__
    assert rec.result_schema is not None
    assert rec.result_schema.__name__ == SyncApplyApplyResult.__name__


def test_apply_action_metadata() -> None:
    """Engine and action strings are exact matches."""
    rec: RegisteredAction = ACTION_REGISTRY.get(_ENGINE, _ACTION)
    assert rec.engine == _ENGINE
    assert rec.action == _ACTION


# ---------------------------------------------------------------------------
# Invalid-args tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_selected_items_missing_item_ids() -> None:
    """mode=selected_items without item_ids raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _ACTION,
            raw_args={
                'reconciliation_run_id': str(uuid.uuid4()),
                'mode': 'selected_items',
            },
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_apply_selected_items_empty_item_ids() -> None:
    """mode=selected_items with item_ids=[] raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _ACTION,
            raw_args={
                'reconciliation_run_id': str(uuid.uuid4()),
                'mode': 'selected_items',
                'item_ids': [],
            },
            ctx=ctx,
        )


@pytest.mark.asyncio
async def test_apply_auto_apply_with_item_ids() -> None:
    """mode=auto_apply with non-empty item_ids raises ActionArgsValidationError."""
    ctx = _make_ctx_mock()
    with pytest.raises(ActionArgsValidationError):
        await ACTION_REGISTRY.dispatch(
            _ENGINE,
            _ACTION,
            raw_args={
                'reconciliation_run_id': str(uuid.uuid4()),
                'mode': 'auto_apply',
                'item_ids': [str(uuid.uuid4())],
            },
            ctx=ctx,
        )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_dry_run_via_registry(session_factory) -> None:  # type: ignore[no-untyped-def]
    """dry_run dispatch returns applied_count=0, failed_count=0, snapshot_ids={}."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        await _seed_delta_item(session, run, status=ReconciliationDeltaItemStatus.pending)
        await session.commit()

    fake_lake_session = MagicMock()
    fake_catalog = MagicMock()

    async with session_factory() as session:
        ctx = _make_ctx(session)

        with (
            patch(
                'src.engines.sync_apply.actions.get_process_lake_session',
                new=AsyncMock(return_value=fake_lake_session),
            ),
            patch(
                'src.engines.sync_apply.actions.get_process_lake_catalog',
                return_value=fake_catalog,
            ),
            patch(
                'src.engines.sync_apply.actions._build_event_service',
                return_value=MagicMock(),
            ),
            patch(
                'src.engines.sync_apply.service.preflight_recover_already_written',
                return_value=PreflightRecoveryResult(recovered_ids=set()),
            ),
        ):
            raw = await ACTION_REGISTRY.dispatch(
                _ENGINE,
                _ACTION,
                raw_args={
                    'reconciliation_run_id': str(run.id),
                    'mode': 'dry_run',
                },
                ctx=ctx,
            )

    assert raw['applied_count'] == 0
    assert raw['failed_count'] == 0
    assert raw['snapshot_ids'] == {}


@pytest.mark.asyncio
async def test_apply_auto_apply_emits_event(session_factory) -> None:  # type: ignore[no-untyped-def]
    """auto_apply with 1 approved CREATE item → applied_count=1, event emitted."""
    from src.engines.sync_apply.lake_writer import RunWriteResult  # noqa: PLC0415
    from src.platform.events.testing import CapturingEventService  # noqa: PLC0415

    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        await _seed_delta_item(session, run, status=ReconciliationDeltaItemStatus.approved)
        run_id: UUID = run.id
        await session.commit()

    capturing = CapturingEventService()
    fake_lake_session = MagicMock()
    fake_catalog = MagicMock()

    stub_write_result = RunWriteResult(
        create_count=1,
        update_count=0,
        revoke_count=0,
        reactivate_count=0,
    )

    async with session_factory() as session:
        ctx = _make_ctx(session)

        with (
            patch(
                'src.engines.sync_apply.actions.get_process_lake_session',
                new=AsyncMock(return_value=fake_lake_session),
            ),
            patch(
                'src.engines.sync_apply.actions.get_process_lake_catalog',
                return_value=fake_catalog,
            ),
            patch(
                'src.engines.sync_apply.actions._build_event_service',
                return_value=capturing,
            ),
            patch(
                'src.engines.sync_apply.service.write_run_batch',
                return_value=stub_write_result,
            ),
            patch(
                'src.engines.sync_apply.service.preflight_recover_already_written',
                return_value=PreflightRecoveryResult(recovered_ids=set()),
            ),
        ):
            raw = await ACTION_REGISTRY.dispatch(
                _ENGINE,
                _ACTION,
                raw_args={
                    'reconciliation_run_id': str(run_id),
                    'mode': 'auto_apply',
                },
                ctx=ctx,
            )
            await session.commit()

    assert raw['applied_count'] == 1
    event_types = [e.event_type for e in capturing.emitted]
    assert any('inventory.access_fact' in t for t in event_types)


# ---------------------------------------------------------------------------
# Commit-ownership test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_action_does_not_commit_session(session_factory) -> None:  # type: ignore[no-untyped-def]
    """After apply action dispatch, session.in_transaction() is still True (runner-owned)."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        await session.commit()

    fake_lake_session = MagicMock()
    fake_catalog = MagicMock()

    async with session_factory() as session:
        ctx = _make_ctx(session)

        with (
            patch(
                'src.engines.sync_apply.actions.get_process_lake_session',
                new=AsyncMock(return_value=fake_lake_session),
            ),
            patch(
                'src.engines.sync_apply.actions.get_process_lake_catalog',
                return_value=fake_catalog,
            ),
            patch(
                'src.engines.sync_apply.actions._build_event_service',
                return_value=MagicMock(),
            ),
            patch(
                'src.engines.sync_apply.service.preflight_recover_already_written',
                return_value=PreflightRecoveryResult(recovered_ids=set()),
            ),
        ):
            await ACTION_REGISTRY.dispatch(
                _ENGINE,
                _ACTION,
                raw_args={
                    'reconciliation_run_id': str(run.id),
                    'mode': 'dry_run',
                },
                ctx=ctx,
            )

        # Runner owns the commit — session must still be in an active transaction
        assert session.in_transaction()
