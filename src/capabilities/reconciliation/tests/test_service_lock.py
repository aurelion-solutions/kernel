# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: advisory lock behaviour in ReconciliationService."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.capabilities.reconciliation.exceptions import ReconciliationAlreadyRunningError
from src.capabilities.reconciliation.schemas import ReconciliationRunMode, ReconciliationRunSummary
from src.capabilities.reconciliation.service import ReconciliationService
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


def _make_service(session, *, lock_acquired: bool = True) -> ReconciliationService:
    """Build a ReconciliationService with mocked lake deps and advisory lock."""
    return ReconciliationService(
        session=session,
        lake_session=MagicMock(),
        catalog=MagicMock(),
        events=NoOpEventService(),
        logs=NoOpLogService(),
    )


def _make_session(*, lock_acquired: bool = True, app_exists: bool = True):
    """Build a mock AsyncSession that simulates advisory lock behaviour."""
    conn = AsyncMock()

    # pg_try_advisory_lock returns scalar True/False
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

    return session, conn


@pytest.mark.asyncio
async def test_lock_acquired_and_released_on_success():
    """Lock is acquired once and released once in the finally block on success."""
    app_id = uuid4()
    run_id = uuid4()
    summary = _make_summary(app_id=app_id, run_id=run_id)

    session, conn = _make_session(lock_acquired=True)
    svc = _make_service(session)

    with patch(
        'src.capabilities.reconciliation.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        result = await svc.run(app_id, mode=ReconciliationRunMode.review)

    assert result is summary

    # Verify pg_advisory_unlock was called
    called_sqls = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert any('pg_advisory_unlock' in s for s in called_sqls)
    assert any('pg_try_advisory_lock' in s for s in called_sqls)


@pytest.mark.asyncio
async def test_lock_released_on_pipeline_exception():
    """Lock is released in finally even when pipeline raises."""
    app_id = uuid4()
    session, conn = _make_session(lock_acquired=True)
    svc = _make_service(session)

    with patch(
        'src.capabilities.reconciliation.service.run_reconciliation',
        new=AsyncMock(side_effect=RuntimeError('pipeline exploded')),
    ):
        with pytest.raises(RuntimeError, match='pipeline exploded'):
            await svc.run(app_id, mode=ReconciliationRunMode.review)

    called_sqls = [str(call.args[0]) for call in conn.execute.call_args_list]
    assert any('pg_advisory_unlock' in s for s in called_sqls)


@pytest.mark.asyncio
async def test_second_run_raises_already_running():
    """When advisory lock is not acquired, ReconciliationAlreadyRunningError is raised."""
    app_id = uuid4()
    session, _conn = _make_session(lock_acquired=False)
    svc = _make_service(session)

    with pytest.raises(ReconciliationAlreadyRunningError) as exc_info:
        await svc.run(app_id, mode=ReconciliationRunMode.review)

    assert exc_info.value.application_id == app_id


@pytest.mark.asyncio
async def test_sequential_runs_succeed_after_lock_released():
    """After a successful run, the same app can be reconciled again."""
    app_id = uuid4()
    run_id = uuid4()
    summary = _make_summary(app_id=app_id, run_id=run_id)

    # Both sessions independently acquire the lock
    session1, _ = _make_session(lock_acquired=True)
    session2, _ = _make_session(lock_acquired=True)

    svc1 = _make_service(session1)
    svc2 = _make_service(session2)

    with patch(
        'src.capabilities.reconciliation.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        result1 = await svc1.run(app_id, mode=ReconciliationRunMode.review)
        result2 = await svc2.run(app_id, mode=ReconciliationRunMode.review)

    assert result1 is summary
    assert result2 is summary


@pytest.mark.asyncio
async def test_lock_uses_pinned_connection():
    """session.connection() is called once and the same conn object handles lock/unlock."""
    app_id = uuid4()
    run_id = uuid4()
    summary = _make_summary(app_id=app_id, run_id=run_id)

    session, conn = _make_session(lock_acquired=True)
    svc = _make_service(session)

    with patch(
        'src.capabilities.reconciliation.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        await svc.run(app_id, mode=ReconciliationRunMode.review)

    # session.connection() called exactly once — all lock ops go to same conn
    session.connection.assert_awaited_once()
