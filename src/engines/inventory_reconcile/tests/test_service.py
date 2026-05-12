# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for ReconciliationService (updated Step 9 — new constructor signature)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.engines.inventory_reconcile.schemas import ReconciliationRunMode, ReconciliationRunSummary
from src.engines.inventory_reconcile.service import ReconciliationService
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService


def _make_summary(facts_revoked: int = 0, facts_errored: int = 0) -> ReconciliationRunSummary:
    return ReconciliationRunSummary(
        run_id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        artifacts_ingested=1,
        facts_created=0,
        facts_updated=0,
        facts_revoked=facts_revoked,
        artifacts_unhandled=0,
        facts_errored=facts_errored,
    )


def _make_session(*, lock_acquired: bool = True, app_exists: bool = True):
    """Build a mock AsyncSession that simulates advisory lock behaviour."""
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


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events) -> EventService:
    return EventService(sink=capturing_events)


@pytest.mark.asyncio
async def test_run_delegates_to_pipeline_and_emits_completed(
    event_service: EventService,
    capturing_events: CapturingEventService,
):
    """run() calls pipeline, emits reconciliation.run.completed event."""
    summary = _make_summary()

    from src.platform.lake.config import LakeSettings

    session_mock = _make_session()
    svc = ReconciliationService(
        session=session_mock,
        lake_session=MagicMock(),
        catalog=MagicMock(),
        events=event_service,
        logs=NoOpLogService(),
        lake_settings=LakeSettings(),
    )

    with patch(
        'src.engines.inventory_reconcile.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        result = await svc.run(summary.application_id, mode=ReconciliationRunMode.review)

    assert result is summary
    completed = capturing_events.filter_by_type('reconciliation.run.completed')
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_run_raises_application_not_found_when_session_get_returns_none(
    event_service: EventService,
):
    """run() raises ApplicationNotFoundError when session.get(Application) returns None."""
    from src.platform.lake.config import LakeSettings

    session_mock = _make_session(app_exists=False)
    svc = ReconciliationService(
        session=session_mock,
        lake_session=MagicMock(),
        catalog=MagicMock(),
        events=event_service,
        logs=NoOpLogService(),
        lake_settings=LakeSettings(),
    )

    with pytest.raises(ApplicationNotFoundError):
        await svc.run(uuid.uuid4(), mode=ReconciliationRunMode.review)


@pytest.mark.asyncio
async def test_emit_completed_uses_correct_routing_key(
    event_service: EventService,
    capturing_events: CapturingEventService,
):
    """run() produces event_type='reconciliation.run.completed' (3-segment)."""
    from src.platform.lake.config import LakeSettings

    summary = _make_summary()
    session_mock = _make_session()
    svc = ReconciliationService(
        session=session_mock,
        lake_session=MagicMock(),
        catalog=MagicMock(),
        events=event_service,
        logs=NoOpLogService(),
        lake_settings=LakeSettings(),
    )

    with patch(
        'src.engines.inventory_reconcile.service.run_reconciliation',
        new=AsyncMock(return_value=summary),
    ):
        await svc.run(summary.application_id, mode=ReconciliationRunMode.review)

    completed = capturing_events.filter_by_type('reconciliation.run.completed')
    assert len(completed) == 1
    assert completed[0].event_type == 'reconciliation.run.completed'
