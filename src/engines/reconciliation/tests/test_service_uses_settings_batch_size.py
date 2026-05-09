# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: ReconciliationService forwards LakeSettings.reconciliation_fetch_batch_size
to pipeline.run_reconciliation (Phase 17 Step 14)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from src.engines.reconciliation.schemas import ReconciliationRunMode, ReconciliationRunSummary
from src.engines.reconciliation.service import ReconciliationService
from src.platform.events.service import NoOpEventService
from src.platform.lake.config import LakeSettings
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


@pytest.mark.asyncio
async def test_run_reconciliation_receives_settings_batch_size():
    """ReconciliationService passes lake_settings.reconciliation_fetch_batch_size to pipeline."""
    app_id = uuid4()
    run_id = uuid4()
    summary = _make_summary(app_id=app_id, run_id=run_id)

    lake_settings = LakeSettings(reconciliation_fetch_batch_size=42)
    session = _make_session()

    svc = ReconciliationService(
        session=session,
        lake_session=MagicMock(),
        catalog=MagicMock(),
        events=NoOpEventService(),
        logs=NoOpLogService(),
        lake_settings=lake_settings,
    )

    with patch(
        'src.engines.reconciliation.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ) as mock_pipeline:
        await svc.run(app_id, mode=ReconciliationRunMode.review)

    mock_pipeline.assert_awaited_once()
    call_kwargs = mock_pipeline.call_args.kwargs
    assert call_kwargs.get('batch_size') == 42, (
        f'Expected batch_size=42, got batch_size={call_kwargs.get("batch_size")}'
    )
