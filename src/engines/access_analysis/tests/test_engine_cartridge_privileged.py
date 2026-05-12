# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: privileged_access detection via cartridge path in ScanEngine.

Two scenarios:
  1. Mock cartridge service — verifies the service is invoked with the real
     fields from loaded EffectiveGrant / Account / Resource and a finding is
     produced when matched=True.
  2. Real cartridge service — pure e2e: seeds privileged candidates, runs
     the engine with the live cartridge, asserts finding kind/severity.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
import uuid

import pytest
from src.engines.access_analysis.engine import ScanEngine
from src.engines.access_analysis.tests.conftest import (
    seed_application,
    seed_pending_scan_run,
    seed_subject,
)
from src.engines.access_effective.models import EffectiveGrant, EffectiveGrantEffect
from src.engines.policy_assessment.cartridge_service import PolicyCartridgeAssessmentService
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput
from src.engines.policy_assessment.schemas import AbstractState, Decision, RiskLevel
from src.inventory.accounts.models import Account
from src.inventory.assessment.findings.models import FindingKind
from src.inventory.assessment.scan_runs.models import ScanRun
from src.inventory.enums import Action
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.resources.models import Resource, ResourcePrivilegeLevel
from src.inventory.subjects.models import SubjectKind
from src.platform.logs.service import NoOpLogService

_AT = datetime(2026, 5, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_account(
    session,
    app_id: uuid.UUID,
    *,
    is_privileged: bool,
    subject_id: uuid.UUID | None = None,
) -> uuid.UUID:
    account = Account(
        application_id=app_id,
        username=f'priv-{uuid.uuid4().hex[:8]}',
        is_privileged=is_privileged,
        subject_id=subject_id,
    )
    session.add(account)
    await session.flush()
    return account.id


async def _seed_resource_with_priv(
    session,
    app_id: uuid.UUID,
    *,
    privilege_level: ResourcePrivilegeLevel | None,
) -> uuid.UUID:
    r = Resource(
        external_id=f'res-{uuid.uuid4().hex[:8]}',
        application_id=app_id,
        kind='role',
        resource_type='role',
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
        privilege_level=privilege_level,
    )
    session.add(r)
    await session.flush()
    return r.id


async def _seed_privileged_grant(
    session,
    *,
    subject_id: uuid.UUID,
    app_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action: Action,
) -> uuid.UUID:
    """Seed Initiative + EffectiveGrant with the given action / resource / account."""
    now = datetime.now(UTC) - timedelta(days=1)
    fact_id = uuid.uuid4()
    initiative = Initiative(
        access_fact_id=fact_id,
        type=InitiativeType.birthright,
        origin='test-origin',
        valid_from=now,
        valid_until=None,
    )
    session.add(initiative)
    await session.flush()

    eg = EffectiveGrant(
        id=uuid.uuid4(),
        subject_id=subject_id,
        subject_kind=SubjectKind.nhi,
        application_id=app_id,
        account_id=account_id,
        resource_id=resource_id,
        action=action,
        effect=EffectiveGrantEffect.allow,
        initiative_type=InitiativeType.birthright,
        initiative_origin='test-origin',
        valid_from=now,
        valid_until=None,
        source_access_fact_id=fact_id,
        source_initiative_id=initiative.id,
        observed_at=now,
        tombstoned_at=None,
    )
    session.add(eg)
    await session.flush()
    return eg.id


# ---------------------------------------------------------------------------
# 1. Mock cartridge service — verifies call contract + context plumbing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cartridge_service_called_with_real_context(session_factory, engine_test_lake_session) -> None:
    """Engine builds context dict from loaded EffectiveGrant + Account + Resource."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        account_id = await _seed_account(session, app_id, is_privileged=True, subject_id=subject_id)
        resource_id = await _seed_resource_with_priv(session, app_id, privilege_level=ResourcePrivilegeLevel.admin)
        await _seed_privileged_grant(
            session,
            subject_id=subject_id,
            app_id=app_id,
            account_id=account_id,
            resource_id=resource_id,
            action=Action.administer,
        )
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
            correlation_id='test-priv-ctx',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    priv_calls = [c for c in mock_svc.evaluate_file.call_args_list if 'privileged' in str(c[0][0])]
    assert len(priv_calls) == 1
    _, ctx = priv_calls[0][0]
    assert ctx['account_is_privileged'] is True
    assert ctx['action'] == 'administer'
    assert ctx['resource_privilege_level'] == 'admin'
    assert result.error is None


@pytest.mark.asyncio
async def test_matched_true_creates_privileged_finding(session_factory, engine_test_lake_session) -> None:
    """matched=True → FindingKind.privileged_access row created."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        account_id = await _seed_account(session, app_id, is_privileged=True, subject_id=subject_id)
        resource_id = await _seed_resource_with_priv(session, app_id, privilege_level=None)
        await _seed_privileged_grant(
            session,
            subject_id=subject_id,
            app_id=app_id,
            account_id=account_id,
            resource_id=resource_id,
            action=Action.read,
        )
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
            correlation_id='test-priv-match',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    priv_findings = [e for e in result.findings_created if e.kind == FindingKind.privileged_access]
    assert len(priv_findings) == 1
    assert priv_findings[0].subject_id == subject_id
    assert priv_findings[0].account_id == account_id
    assert priv_findings[0].severity.value == 'high'  # DEFAULT_PRIVILEGED_SEVERITY fallback


@pytest.mark.asyncio
async def test_matched_false_creates_no_finding(session_factory, engine_test_lake_session) -> None:
    """matched=False → no privileged_access finding."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        account_id = await _seed_account(session, app_id, is_privileged=False, subject_id=subject_id)
        resource_id = await _seed_resource_with_priv(session, app_id, privilege_level=None)
        await _seed_privileged_grant(
            session,
            subject_id=subject_id,
            app_id=app_id,
            account_id=account_id,
            resource_id=resource_id,
            action=Action.read,
        )
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
            correlation_id='test-priv-no-match',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    priv_findings = [e for e in result.findings_created if e.kind == FindingKind.privileged_access]
    assert priv_findings == []


@pytest.mark.asyncio
async def test_severity_from_cartridge_decision_overrides_default(session_factory, engine_test_lake_session) -> None:
    """Decision.risk_level wins over DEFAULT_PRIVILEGED_SEVERITY."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        account_id = await _seed_account(session, app_id, is_privileged=True, subject_id=subject_id)
        resource_id = await _seed_resource_with_priv(session, app_id, privilege_level=None)
        await _seed_privileged_grant(
            session,
            subject_id=subject_id,
            app_id=app_id,
            account_id=account_id,
            resource_id=resource_id,
            action=Action.read,
        )
        run = await seed_pending_scan_run(session)
        await session.commit()

    mock_svc = MagicMock(spec=PolicyCartridgeAssessmentService)
    mock_svc.evaluate_file.return_value = PolicyAssessmentOutput(
        matched=True,
        decision=Decision(abstract_state=AbstractState.suspended, risk_level=RiskLevel.critical),
    )

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine(cartridge_service=mock_svc)
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='test-priv-decision',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    priv_findings = [e for e in result.findings_created if e.kind == FindingKind.privileged_access]
    assert len(priv_findings) == 1
    assert priv_findings[0].severity.value == 'critical'


# ---------------------------------------------------------------------------
# 2. Real cartridge service — e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_cartridge_matches_account_is_privileged(session_factory, engine_test_lake_session) -> None:
    """E2E: account_is_privileged=true triggers the cartridge regardless of action."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        account_id = await _seed_account(session, app_id, is_privileged=True, subject_id=subject_id)
        resource_id = await _seed_resource_with_priv(session, app_id, privilege_level=ResourcePrivilegeLevel.read)
        await _seed_privileged_grant(
            session,
            subject_id=subject_id,
            app_id=app_id,
            account_id=account_id,
            resource_id=resource_id,
            action=Action.read,
        )
        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine()
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='e2e-priv-account',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    assert result.error is None
    priv_findings = [e for e in result.findings_created if e.kind == FindingKind.privileged_access]
    assert len(priv_findings) == 1
    # Severity comes from privileged_access.yaml decision.risk_level=high.
    assert priv_findings[0].severity.value == 'high'


@pytest.mark.asyncio
async def test_real_cartridge_matches_administer_on_admin_resource(session_factory, engine_test_lake_session) -> None:
    """E2E: action=administer + resource_privilege_level=admin triggers the cartridge."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        account_id = await _seed_account(session, app_id, is_privileged=False, subject_id=subject_id)
        resource_id = await _seed_resource_with_priv(session, app_id, privilege_level=ResourcePrivilegeLevel.admin)
        await _seed_privileged_grant(
            session,
            subject_id=subject_id,
            app_id=app_id,
            account_id=account_id,
            resource_id=resource_id,
            action=Action.administer,
        )
        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine()
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='e2e-priv-admin-resource',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    assert result.error is None
    priv_findings = [e for e in result.findings_created if e.kind == FindingKind.privileged_access]
    assert len(priv_findings) == 1
    assert priv_findings[0].severity.value == 'high'


@pytest.mark.asyncio
async def test_real_cartridge_no_match_for_plain_read(session_factory, engine_test_lake_session) -> None:
    """E2E: account_is_privileged=false + action=read + non-admin resource → no finding."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        account_id = await _seed_account(session, app_id, is_privileged=False, subject_id=subject_id)
        resource_id = await _seed_resource_with_priv(session, app_id, privilege_level=ResourcePrivilegeLevel.read)
        await _seed_privileged_grant(
            session,
            subject_id=subject_id,
            app_id=app_id,
            account_id=account_id,
            resource_id=resource_id,
            action=Action.read,
        )
        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine()
        result = await engine.run(
            session,
            run,
            at=_AT,
            correlation_id='e2e-priv-no-match',
            lake_session=engine_test_lake_session,
            log_service=NoOpLogService(),
            pg_any_array_max_size=25000,
        )
        await session.commit()

    assert result.error is None
    priv_findings = [e for e in result.findings_created if e.kind == FindingKind.privileged_access]
    assert priv_findings == []
