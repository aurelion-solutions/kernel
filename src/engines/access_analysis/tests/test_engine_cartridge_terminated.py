# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: terminated_subject_access detection via cartridge path in ScanEngine.

Two scenarios:
  1. Mock cartridge service — verifies the service is invoked with the real
     subject status from loaded data and a finding is produced when matched=True.
  2. Real cartridge service — pure e2e: seeds a terminal-subject Account,
     runs engine with the live cartridge, asserts finding created.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
import uuid

import pytest
from src.engines.access_analysis.engine import ScanEngine
from src.engines.access_analysis.tests.conftest import (
    seed_application,
    seed_pending_scan_run,
    seed_subject,
)
from src.engines.policy_assessment.cartridge_service import PolicyCartridgeAssessmentService
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput
from src.engines.policy_assessment.schemas import AbstractState, Decision, RiskLevel
from src.inventory.accounts.models import Account
from src.inventory.assessment.findings.models import FindingKind
from src.inventory.assessment.scan_runs.models import ScanRun
from src.platform.logs.service import NoOpLogService

_AT = datetime(2026, 5, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_terminal_account(
    session,
    app_id: uuid.UUID,
    terminal_status: str = 'expired',
) -> tuple[uuid.UUID, uuid.UUID]:
    """Seed NHI subject with terminal status + linked account. Returns (account_id, subject_id)."""
    subject_id = await seed_subject(session, status=terminal_status)
    account = Account(
        application_id=app_id,
        username=f'term-{uuid.uuid4().hex[:8]}',
        subject_id=subject_id,
    )
    session.add(account)
    await session.flush()
    return account.id, subject_id


# ---------------------------------------------------------------------------
# 1. Mock cartridge service — verifies call contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cartridge_service_called_for_terminated_account(session_factory, engine_test_lake_session) -> None:
    """Engine calls cartridge_service.evaluate_file for each terminated candidate."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        account_id, subject_id = await _seed_terminal_account(session, app_id, terminal_status='expired')
        run = await seed_pending_scan_run(session)
        await session.commit()

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(matched=True)

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine(cartridge_service=mock_svc)
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='test-corr',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    mock_svc.evaluate_file.assert_called()
    terminated_calls = [
        call for call in mock_svc.evaluate_file.call_args_list if call[0][0].name == 'terminated_subject_access.yaml'
    ]
    assert len(terminated_calls) == 1
    call_path, call_ctx = terminated_calls[0][0]
    assert call_path.name == 'terminated_subject_access.yaml'
    assert call_ctx['subject']['status'] == 'expired'
    assert result.error is None
    assert result.findings_total == 1
    findings = [e for e in result.findings_created if e.kind == FindingKind.terminated_access]
    assert len(findings) == 1
    assert findings[0].account_id == account_id
    assert findings[0].subject_id == subject_id


@pytest.mark.asyncio
async def test_no_finding_when_cartridge_returns_not_matched(session_factory, engine_test_lake_session) -> None:
    """Engine skips finding creation when cartridge returns matched=False."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        await _seed_terminal_account(session, app_id, terminal_status='expired')
        run = await seed_pending_scan_run(session)
        await session.commit()

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(matched=False)

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine(cartridge_service=mock_svc)
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='test-corr',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    assert result.error is None
    terminated_findings = [e for e in result.findings_created if e.kind == FindingKind.terminated_access]
    assert terminated_findings == []


# ---------------------------------------------------------------------------
# 2. Real cartridge service — e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminated_scan_produces_finding_via_real_cartridge(session_factory, engine_test_lake_session) -> None:
    """E2E: real cartridge loaded from YAML, terminal account detected, finding created."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        account_id, subject_id = await _seed_terminal_account(session, app_id, terminal_status='expired')
        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine()
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='e2e-terminated',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    assert result.error is None
    findings = [e for e in result.findings_created if e.kind == FindingKind.terminated_access]
    assert len(findings) == 1
    emission = findings[0]
    assert emission.account_id == account_id
    assert emission.subject_id == subject_id
    # Severity comes from terminated_subject_access.yaml (decision.risk_level=critical),
    # not from DEFAULT_TERMINATED_SEVERITY (which is high).
    assert emission.severity.value == 'critical'


@pytest.mark.asyncio
async def test_terminated_cartridge_all_terminal_statuses_produce_findings(
    session_factory, engine_test_lake_session
) -> None:
    """Each subject kind's terminal statuses all produce findings via the cartridge."""
    terminal_statuses = ['expired', 'locked']  # NHI statuses seeded by seed_subject (kind=nhi)

    for status in terminal_statuses:
        async with session_factory() as session:
            app_id = await seed_application(session)
            account_id, _ = await _seed_terminal_account(session, app_id, terminal_status=status)
            run = await seed_pending_scan_run(session)
            await session.commit()

        async with session_factory() as session:
            run = await session.get(ScanRun, run.id)
            engine = ScanEngine()
            result = await engine.run(
                session,
                run,
                at=_AT,
                correlation_id=f'e2e-{status}',
                lake_session=engine_test_lake_session,
                log_service=NoOpLogService(),
                pg_any_array_max_size=25000,
            )
            await session.commit()

        findings = [e for e in result.findings_created if e.kind == FindingKind.terminated_access]
        assert len(findings) == 1, f'Expected 1 finding for status={status}, got {len(findings)}'


# ---------------------------------------------------------------------------
# 3. Severity sourcing — cartridge Decision vs default fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_severity_from_cartridge_decision_overrides_default(session_factory, engine_test_lake_session) -> None:
    """When the cartridge returns Decision.risk_level, it wins over DEFAULT_TERMINATED_SEVERITY."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        await _seed_terminal_account(session, app_id, terminal_status='expired')
        run = await seed_pending_scan_run(session)
        await session.commit()

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(
        matched=True,
        decision=Decision(abstract_state=AbstractState.suspended, risk_level=RiskLevel.medium),
    )

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine(cartridge_service=mock_svc)
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='test-severity-decision',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    findings = [e for e in result.findings_created if e.kind == FindingKind.terminated_access]
    assert len(findings) == 1
    assert findings[0].severity.value == 'medium'
    assert result.findings_by_severity == {'medium': 1}


@pytest.mark.asyncio
async def test_severity_falls_back_to_default_when_no_decision(session_factory, engine_test_lake_session) -> None:
    """When the cartridge returns matched=True without a Decision, DEFAULT_TERMINATED_SEVERITY (high) applies."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        await _seed_terminal_account(session, app_id, terminal_status='expired')
        run = await seed_pending_scan_run(session)
        await session.commit()

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(matched=True)

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine(cartridge_service=mock_svc)
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='test-severity-fallback',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    findings = [e for e in result.findings_created if e.kind == FindingKind.terminated_access]
    assert len(findings) == 1
    assert findings[0].severity.value == 'high'  # DEFAULT_TERMINATED_SEVERITY
