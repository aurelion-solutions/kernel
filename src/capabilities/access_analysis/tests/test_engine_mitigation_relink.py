# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine mitigation relink tests — open→mitigated on second run when active mitigation appears."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.engine import ScanEngine
from src.capabilities.access_analysis.findings.models import Finding, FindingKind, FindingStatus
from src.capabilities.access_analysis.mitigation_controls.models import (
    MitigationControl,
    MitigationControlType,
)
from src.capabilities.access_analysis.mitigations.models import Mitigation, MitigationStatus
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunTrigger
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
    seed_scope_key,
    seed_sod_rule,
    seed_subject,
)


async def _seed_sod_scenario(session_factory):
    """Seed a SoD violation and return (subject_id, rule_id, subject2_id for owner)."""
    async with session_factory() as session:
        app_id = await seed_application(session)
        subject_id = await seed_subject(session)
        owner_id = await seed_subject(session)
        scope_key_id = await seed_scope_key(session)

        cap_a_id = await seed_capability(session, f'cap_mit_a_{uuid.uuid4().hex[:6]}')
        cap_b_id = await seed_capability(session, f'cap_mit_b_{uuid.uuid4().hex[:6]}')
        rule_id = await seed_sod_rule(session, severity=SodSeverity.high)

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

        await session.commit()

    return subject_id, rule_id, owner_id


@pytest.mark.asyncio
async def test_mitigation_relink_open_to_mitigated(session_factory) -> None:
    """Run 1: finding persisted as open. Mitigation activated. Run 2: status flips to mitigated."""
    subject_id, rule_id, owner_id = await _seed_sod_scenario(session_factory)

    # Run 1 — no mitigation yet
    async with session_factory() as session:
        run1 = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run1)
        await session.flush()
        await session.refresh(run1)
        engine = ScanEngine()
        result1 = await engine.run(session, run1, at=datetime.now(UTC), correlation_id='mit-run1')
        await session.commit()

    assert result1.error is None
    assert len(result1.findings_created) >= 1

    # Find the persisted Finding
    async with session_factory() as session:
        findings_result = await session.execute(sa.select(Finding).where(Finding.kind == FindingKind.sod))
        sod_findings = findings_result.scalars().all()
    assert len(sod_findings) >= 1
    sod_finding = sod_findings[0]
    assert sod_finding.status == FindingStatus.open
    assert sod_finding.active_mitigation_id is None

    # Activate a mitigation between runs
    now = datetime.now(UTC)
    async with session_factory() as session:
        ctrl = MitigationControl(
            code=f'CTRL_{uuid.uuid4().hex[:8]}',
            name='Test Control',
            type=MitigationControlType.compensating_process,
            description=None,
        )
        session.add(ctrl)
        await session.flush()

        mit = Mitigation(
            rule_id=rule_id,
            control_id=ctrl.id,
            subject_id=subject_id,
            scope_key_id=None,
            scope_value=None,
            status=MitigationStatus.active,
            valid_from=now - timedelta(hours=1),
            valid_until=None,
            owner_id=owner_id,
        )
        session.add(mit)
        await session.flush()
        mit_id = mit.id
        await session.commit()

    # Run 2 — mitigation is now active
    async with session_factory() as session:
        run2 = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run2)
        await session.flush()
        await session.refresh(run2)
        engine = ScanEngine()
        at = datetime.now(UTC)
        result2 = await engine.run(session, run2, at=at, correlation_id='mit-run2')
        await session.commit()

    assert result2.error is None
    assert len(result2.findings_reused) >= 1
    assert len(result2.findings_status_changed) >= 1

    # The status_changed emission should reference the mitigation
    sc = result2.findings_status_changed[0]
    assert sc.from_status == FindingStatus.open
    assert sc.to_status == FindingStatus.mitigated
    assert sc.status_reason == 'mitigation_activated'
    assert sc.active_mitigation_id == mit_id

    # Verify DB state
    async with session_factory() as session:
        updated_finding = await session.get(Finding, sod_finding.id)
    assert updated_finding is not None
    assert updated_finding.status == FindingStatus.mitigated
    assert updated_finding.active_mitigation_id == mit_id
    assert updated_finding.status_changed_at is not None
    assert updated_finding.status_reason == 'mitigation_activated'
