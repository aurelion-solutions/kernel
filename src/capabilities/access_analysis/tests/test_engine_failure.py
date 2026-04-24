# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine failure tests — bulk-loader raises → ScanRun status=failed, scan.failed emitted."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.findings.models import Finding
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunStatus
from src.capabilities.access_analysis.service import ScanOrchestrationService
from src.capabilities.access_analysis.tests.conftest import seed_pending_scan_run
from src.platform.events.testing import CapturingEventService


@pytest.mark.asyncio
async def test_engine_failure_sets_status_failed(session_factory) -> None:
    """When the engine's bulk-loader raises, ScanRun.status becomes failed."""
    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    capturing = CapturingEventService()

    with patch(
        'src.capabilities.access_analysis.engine.load_sod_rules',
        new_callable=AsyncMock,
        side_effect=RuntimeError('Simulated loader failure'),
    ):
        async with session_factory() as session:
            run = await session.get(ScanRun, run.id)
            orch = ScanOrchestrationService(session=session, events=capturing)
            updated = await orch.run_scan(run.id)
            await session.commit()

    assert updated.status == ScanRunStatus.failed
    assert updated.error_message is not None
    assert 'Simulated loader failure' in updated.error_message


@pytest.mark.asyncio
async def test_engine_failure_emits_scan_failed_event(session_factory) -> None:
    """scan.failed is emitted with error_class and error_message on failure."""
    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    capturing = CapturingEventService()

    with patch(
        'src.capabilities.access_analysis.engine.load_sod_rules',
        new_callable=AsyncMock,
        side_effect=RuntimeError('Boom!'),
    ):
        async with session_factory() as session:
            run = await session.get(ScanRun, run.id)
            orch = ScanOrchestrationService(session=session, events=capturing)
            await orch.run_scan(run.id)
            await session.commit()

    failed_events = capturing.filter_by_type('access_analysis.scan.failed')
    assert len(failed_events) == 1
    payload = failed_events[0].payload
    assert payload['error_class'] == 'RuntimeError'
    assert 'Boom!' in payload['error_message']

    started_events = capturing.filter_by_type('access_analysis.scan.started')
    assert len(started_events) == 1
    assert failed_events[0].causation_id == started_events[0].event_id


@pytest.mark.asyncio
async def test_engine_failure_no_half_written_findings(session_factory) -> None:
    """On failure, no Finding rows are written to the database."""
    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    run_id = run.id

    with patch(
        'src.capabilities.access_analysis.engine.load_sod_rules',
        new_callable=AsyncMock,
        side_effect=RuntimeError('Abort'),
    ):
        async with session_factory() as session:
            run = await session.get(ScanRun, run.id)
            orch = ScanOrchestrationService(session=session, events=CapturingEventService())
            await orch.run_scan(run.id)
            await session.commit()

    async with session_factory() as session:
        result = await session.execute(
            sa.select(sa.func.count()).select_from(Finding).where(Finding.scan_run_id == run_id)
        )
        count = result.scalar_one()

    assert count == 0
