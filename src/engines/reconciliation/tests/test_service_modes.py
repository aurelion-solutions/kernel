# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: mode dispatch in ReconciliationService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.engines.reconciliation.models import ReconciliationRunStatus
from src.engines.reconciliation.schemas import ReconciliationRunMode, ReconciliationRunSummary
from src.engines.reconciliation.service import ReconciliationService
from src.platform.events.service import NoOpEventService
from src.platform.logs.service import NoOpLogService


def _make_summary(app_id=None, run_id=None) -> ReconciliationRunSummary:
    return ReconciliationRunSummary(
        run_id=run_id or uuid4(),
        application_id=app_id or uuid4(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        artifacts_ingested=0,
        facts_created=0,
        facts_updated=0,
        facts_revoked=0,
        artifacts_unhandled=0,
    )


def _make_session(*, lock_acquired: bool = True, app_exists: bool = True):
    conn = AsyncMock()

    lock_key_result = AsyncMock()
    lock_key_result.scalar_one = MagicMock(return_value=12345)

    acquire_result = AsyncMock()
    acquire_result.scalar_one = MagicMock(return_value=lock_acquired)

    release_result = AsyncMock()

    def execute_side_effect(stmt, params=None):
        sql_str = str(stmt)
        if 'md5' in sql_str:
            return lock_key_result
        if 'pg_try_advisory_lock' in sql_str:
            return acquire_result
        if 'pg_advisory_unlock' in sql_str:
            return release_result
        return AsyncMock()

    conn.execute = AsyncMock(side_effect=execute_side_effect)

    session = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock() if app_exists else None)
    session.connection = AsyncMock(return_value=conn)

    return session


def _make_service(session) -> ReconciliationService:
    from src.platform.lake.config import LakeSettings

    return ReconciliationService(
        session=session,
        lake_session=MagicMock(),
        catalog=MagicMock(),
        events=NoOpEventService(),
        logs=NoOpLogService(),
        lake_settings=LakeSettings(),
    )


@pytest.mark.asyncio
async def test_mode_review_ends_pending_apply():
    """mode=review: pipeline runs; status stays pending_apply (no override)."""
    app_id = uuid4()
    run_id = uuid4()
    summary = _make_summary(app_id=app_id, run_id=run_id)

    session = _make_session()
    svc = _make_service(session)

    with (
        patch(
            'src.engines.reconciliation.service.run_reconciliation',
            new=AsyncMock(return_value=summary),
        ) as mock_pipeline,
        patch('src.engines.reconciliation.service.update_run_status') as mock_update,
    ):
        result = await svc.run(app_id, mode=ReconciliationRunMode.review)

    assert result is summary
    mock_pipeline.assert_awaited_once()
    # update_run_status should NOT be called for review mode override
    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_mode_dry_run_overrides_status_to_dry_run_completed():
    """mode=dry_run: pipeline runs; update_run_status called with dry_run_completed."""
    app_id = uuid4()
    run_id = uuid4()
    summary = _make_summary(app_id=app_id, run_id=run_id)

    session = _make_session()
    svc = _make_service(session)

    with (
        patch(
            'src.engines.reconciliation.service.run_reconciliation',
            new=AsyncMock(return_value=summary),
        ) as mock_pipeline,
        patch(
            'src.engines.reconciliation.service.update_run_status',
            new=AsyncMock(),
        ) as mock_update,
    ):
        result = await svc.run(app_id, mode=ReconciliationRunMode.dry_run)

    assert result is summary
    mock_pipeline.assert_awaited_once()
    mock_update.assert_awaited_once()
    call_kwargs = mock_update.call_args
    assert call_kwargs.kwargs['status'] == ReconciliationRunStatus.dry_run_completed


@pytest.mark.asyncio
async def test_mode_auto_apply_runs_pipeline():
    """mode=auto_apply: Step 12 — pipeline runs normally; SyncApplyService handles apply."""
    app_id = uuid4()
    run_id = uuid4()
    summary = _make_summary(app_id=app_id, run_id=run_id)

    session = _make_session()
    svc = _make_service(session)

    with (
        patch(
            'src.engines.reconciliation.service.run_reconciliation',
            new=AsyncMock(return_value=summary),
        ) as mock_pipeline,
        patch('src.engines.reconciliation.service.update_run_status') as mock_update,
    ):
        result = await svc.run(app_id, mode=ReconciliationRunMode.auto_apply)

    assert result is summary
    # Pipeline IS called for auto_apply (Step 12: service no longer raises 501)
    mock_pipeline.assert_awaited_once()
    # No status override for auto_apply (pending_apply is left for SyncApplyService)
    mock_update.assert_not_called()
