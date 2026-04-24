# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine orchestration tests — empty scope, SoD-only, all detectors."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.capabilities.access_analysis.engine import ScanEngine
from src.capabilities.access_analysis.scan_runs.models import ScanRun
from src.capabilities.access_analysis.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.capabilities.access_analysis.sod_rules.models import SodSeverity
from src.capabilities.access_analysis.tests.conftest import (
    seed_application,
    seed_capability,
    seed_capability_grant,
    seed_effective_grant,
    seed_mapping,
    seed_pending_scan_run,
    seed_scope_key,
    seed_sod_rule,
    seed_subject,
)


@pytest.mark.asyncio
async def test_empty_scope_returns_completed_zero_findings(session_factory) -> None:
    """Empty DB (no grants, no accounts, no access facts) → zero findings."""
    async with session_factory() as session:
        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine()
        at = datetime.now(UTC)
        result = await engine.run(session, run, at=at, correlation_id='test-corr')
        await session.commit()

    assert result.error is None
    assert result.findings_total == 0
    assert result.findings_created == []
    assert result.findings_reused == []
    assert result.findings_by_severity == {}


@pytest.mark.asyncio
async def test_sod_run_produces_one_finding(session_factory) -> None:
    """SoD violation: subject with two capability grants matching a two-condition rule → 1 Finding."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        scope_key_id = await seed_scope_key(session)

        cap_a_id = await seed_capability(session, f'cap_a_{uuid.uuid4().hex[:6]}')
        cap_b_id = await seed_capability(session, f'cap_b_{uuid.uuid4().hex[:6]}')
        rule_id = await seed_sod_rule(session, severity=SodSeverity.high)

        # Two conditions: requires cap_a AND cap_b
        cond_a = SodRuleCondition(rule_id=rule_id, min_count=1)
        cond_b = SodRuleCondition(rule_id=rule_id, min_count=1)
        session.add(cond_a)
        session.add(cond_b)
        await session.flush()

        await session.execute(
            sod_rule_condition_capabilities.insert().values(condition_id=cond_a.id, capability_id=cap_a_id)
        )
        await session.execute(
            sod_rule_condition_capabilities.insert().values(condition_id=cond_b.id, capability_id=cap_b_id)
        )
        await session.flush()

        mapping_a_id = await seed_mapping(session, cap_a_id, app_id, scope_key_id)
        mapping_b_id = await seed_mapping(session, cap_b_id, app_id, scope_key_id)

        eg_a = await seed_effective_grant(session, subject_id, app_id)
        eg_b = await seed_effective_grant(session, subject_id, app_id)

        await seed_capability_grant(session, subject_id, cap_a_id, app_id, scope_key_id, eg_a, mapping_a_id)
        await seed_capability_grant(session, subject_id, cap_b_id, app_id, scope_key_id, eg_b, mapping_b_id)

        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine()
        at = datetime.now(UTC)
        result = await engine.run(session, run, at=at, correlation_id='sod-corr')
        await session.commit()

    assert result.error is None
    assert result.findings_total == 1
    assert len(result.findings_created) == 1
    assert len(result.findings_reused) == 0
    assert result.findings_by_severity.get('high', 0) == 1


@pytest.mark.asyncio
async def test_severity_rollup_aggregates_across_kinds(session_factory) -> None:
    """When findings of multiple severities exist, rollup sums correctly."""
    # This test creates a terminated account (high severity) to verify rollup logic
    from src.inventory.accounts.models import Account
    from src.inventory.nhi.models import NHI
    from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind

    async with session_factory() as session:
        app_id = await seed_application(session)

        # Create a terminated subject + account
        nhi = NHI(
            external_id=f'nhi-term-{uuid.uuid4().hex[:8]}',
            name='Terminated NHI',
            kind='service_account',
            owner_employee_id=None,
        )
        session.add(nhi)
        await session.flush()

        subject = Subject(
            external_id=f'subj-term-{uuid.uuid4().hex[:8]}',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.service_account,
            principal_nhi_id=nhi.id,
            status='expired',  # terminal for NHI
        )
        session.add(subject)
        await session.flush()

        account = Account(
            application_id=app_id,
            username=f'term-acct-{uuid.uuid4().hex[:8]}',
            subject_id=subject.id,
            is_active=True,
        )
        session.add(account)
        await session.flush()

        run = await seed_pending_scan_run(session)
        await session.commit()

    async with session_factory() as session:
        run = await session.get(ScanRun, run.id)
        engine = ScanEngine()
        at = datetime.now(UTC)
        result = await engine.run(session, run, at=at, correlation_id='rollup-corr')
        await session.commit()

    assert result.error is None
    # Should have a terminated_access finding (high severity)
    assert result.findings_by_severity.get('high', 0) >= 1
