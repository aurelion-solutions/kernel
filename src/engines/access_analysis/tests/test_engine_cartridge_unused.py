# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: unused_access detection via cartridge path in ScanEngine.

load_unused_inputs reads from Iceberg which requires complex seeding.
These tests mock load_unused_inputs to inject synthetic AccessFactView objects,
then verify the cartridge service receives the correct computed context and that
findings are created / suppressed accordingly.

Two scenarios:
  1. Mock cartridge service — verifies computed days_since_last_use in context.
  2. Real cartridge service (mock loader) — e2e through the YAML evaluator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.engines.access_analysis.engine import ScanEngine
from src.engines.access_analysis.tests.conftest import seed_pending_scan_run, seed_subject
from src.engines.policy_assessment.cartridge_service import PolicyCartridgeAssessmentService
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput
from src.engines.policy_assessment.policy_types.access_risk.evaluator import AccessFactView
from src.engines.policy_assessment.schemas import AbstractState, Decision, RiskLevel
from src.inventory.assessment.findings.models import FindingKind
from src.inventory.assessment.scan_runs.models import ScanRun
from src.platform.logs.service import NoOpLogService

_AT = datetime(2026, 5, 1, tzinfo=UTC)
_LOAD_PATH = 'src.engines.access_analysis.engine.load_unused_inputs'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact(
    subject_id: uuid.UUID,
    last_seen: datetime | None,
    valid_from: datetime | None = None,
) -> AccessFactView:
    return AccessFactView(
        id=uuid.uuid4(),
        subject_id=subject_id,
        account_id=None,
        resource_id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        valid_from=valid_from or (_AT - timedelta(days=200)),
        last_seen=last_seen,
    )


# ---------------------------------------------------------------------------
# 1. Mock cartridge service — verifies call contract + context computation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cartridge_called_with_computed_days_last_seen(session_factory, engine_test_lake_session) -> None:
    """days_since_last_use is computed from real last_seen, not hardcoded."""
    async with session_factory() as session:
        subject_id = await seed_subject(session)
        run = await seed_pending_scan_run(session)
        await session.commit()

    fact = _make_fact(subject_id=subject_id, last_seen=_AT - timedelta(days=120))

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(matched=True)

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine(cartridge_service=mock_svc)
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='test',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    unused_calls = [c for c in mock_svc.evaluate_file.call_args_list if 'unused' in str(c[0][0])]
    assert len(unused_calls) == 1
    _, ctx = unused_calls[0][0]
    assert ctx['days_since_last_use'] == 120
    assert result.error is None


@pytest.mark.asyncio
async def test_cartridge_uses_valid_from_when_last_seen_is_none(session_factory, engine_test_lake_session) -> None:
    """Fallback: days_since_last_use computed from valid_from when last_seen is None."""
    fact = _make_fact(subject_id=uuid.uuid4(), last_seen=None, valid_from=_AT - timedelta(days=150))

    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(matched=False)

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine(cartridge_service=mock_svc)
            await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='test-fallback',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    unused_calls = [c for c in mock_svc.evaluate_file.call_args_list if 'unused' in str(c[0][0])]
    assert len(unused_calls) == 1
    _, ctx = unused_calls[0][0]
    assert ctx['days_since_last_use'] == 150


@pytest.mark.asyncio
async def test_matched_true_without_decision_uses_default_severity(session_factory, engine_test_lake_session) -> None:
    """When the cartridge output carries no Decision, the engine falls back to DEFAULT_UNUSED_SEVERITY."""
    async with session_factory() as session:
        subject_id = await seed_subject(session)
        run = await seed_pending_scan_run(session)
        await session.commit()

    fact = _make_fact(subject_id=subject_id, last_seen=_AT - timedelta(days=120))
    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(matched=True)

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine(cartridge_service=mock_svc)
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='test-match',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    unused_findings = [e for e in result.findings_created if e.kind == FindingKind.unused_access]
    assert len(unused_findings) == 1
    assert unused_findings[0].severity.value == 'low'  # DEFAULT_UNUSED_SEVERITY


@pytest.mark.asyncio
async def test_severity_comes_from_cartridge_decision(session_factory, engine_test_lake_session) -> None:
    """When the cartridge output carries Decision.risk_level, that value wins over the default."""
    async with session_factory() as session:
        subject_id = await seed_subject(session)
        run = await seed_pending_scan_run(session)
        await session.commit()

    fact = _make_fact(subject_id=subject_id, last_seen=_AT - timedelta(days=120))
    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(
        matched=True,
        decision=Decision(abstract_state=AbstractState.suspended, risk_level=RiskLevel.critical),
    )

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine(cartridge_service=mock_svc)
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='test-cartridge-severity',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    unused_findings = [e for e in result.findings_created if e.kind == FindingKind.unused_access]
    assert len(unused_findings) == 1
    assert unused_findings[0].severity.value == 'critical'
    assert result.findings_by_severity == {'critical': 1}


@pytest.mark.asyncio
async def test_matched_false_creates_no_finding(session_factory, engine_test_lake_session) -> None:
    """matched=False produces no finding."""
    fact = _make_fact(subject_id=uuid.uuid4(), last_seen=_AT - timedelta(days=120))

    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(matched=False)

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine(cartridge_service=mock_svc)
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='test-no-match',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    unused_findings = [e for e in result.findings_created if e.kind == FindingKind.unused_access]
    assert unused_findings == []


# ---------------------------------------------------------------------------
# 2. Real cartridge service (mock loader) — e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_cartridge_matches_over_threshold(session_factory, engine_test_lake_session) -> None:
    """E2E: real unused_access.yaml evaluates days_since_last_use > 90 as matched=True."""
    async with session_factory() as session:
        subject_id = await seed_subject(session)
        run = await seed_pending_scan_run(session)
        await session.commit()

    fact = _make_fact(subject_id=subject_id, last_seen=_AT - timedelta(days=120))

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine()
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='e2e-unused',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    assert result.error is None
    unused_findings = [e for e in result.findings_created if e.kind == FindingKind.unused_access]
    assert len(unused_findings) == 1
    # Severity comes from unused_access.yaml (decision.risk_level=medium), not the Python default.
    assert unused_findings[0].severity.value == 'medium'


@pytest.mark.asyncio
async def test_real_cartridge_no_match_under_threshold(session_factory, engine_test_lake_session) -> None:
    """E2E: days_since_last_use == 30 is below threshold (90), no finding created."""
    fact = _make_fact(subject_id=uuid.uuid4(), last_seen=_AT - timedelta(days=30))

    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine()
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='e2e-unused-under',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    assert result.error is None
    unused_findings = [e for e in result.findings_created if e.kind == FindingKind.unused_access]
    assert unused_findings == []


@pytest.mark.asyncio
async def test_real_cartridge_fallback_valid_from_over_threshold(session_factory, engine_test_lake_session) -> None:
    """E2E: last_seen=None, valid_from 120 days ago → matched=True via fallback."""
    async with session_factory() as session:
        subject_id = await seed_subject(session)
        run = await seed_pending_scan_run(session)
        await session.commit()

    fact = _make_fact(subject_id=subject_id, last_seen=None, valid_from=_AT - timedelta(days=120))

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        with patch(_LOAD_PATH, new_callable=AsyncMock, return_value=[fact]):
            engine = ScanEngine()
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id='e2e-unused-fallback',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
        await session.commit()

    assert result.error is None
    unused_findings = [e for e in result.findings_created if e.kind == FindingKind.unused_access]
    assert len(unused_findings) == 1
    assert unused_findings[0].severity.value == 'medium'
